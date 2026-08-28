import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.limiter import limiter
from app.core.notifications import notify
from app.models import Complaint, ParentStudentLink, User
from app.schemas.communication import ComplaintCreate, ComplaintUpdate, ComplaintOut

router = APIRouter(prefix="/api/complaints", tags=["complaints"], dependencies=[Depends(check_license)])


def _complaint_out(db: Session, complaint: Complaint) -> ComplaintOut:
    """Joins in submitter/student names — ComplaintOut needs them (the
    Coordinator's resolution center shows who filed each complaint, not a
    raw UUID), same convention as SubjectRequestReviewRow."""
    submitter = db.query(User).filter(User.id == complaint.submitted_by).first()
    student = None
    if complaint.student_id and complaint.student_id != complaint.submitted_by:
        student = db.query(User).filter(User.id == complaint.student_id).first()
    elif complaint.student_id:
        student = submitter

    return ComplaintOut(
        id=complaint.id,
        submitted_by=complaint.submitted_by,
        submitted_by_name=submitter.full_name if submitter else "Unknown",
        submitted_by_role=submitter.role if submitter else "unknown",
        student_id=complaint.student_id,
        student_name=student.full_name if student else None,
        subject_of_complaint=complaint.subject_of_complaint,
        description=complaint.description,
        status=complaint.status,
        resolved_by=complaint.resolved_by,
        resolved_at=complaint.resolved_at,
        resolution_message=complaint.resolution_message,
        created_at=complaint.created_at,
    )


@router.post("", response_model=ComplaintOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def submit_complaint(
    request: Request,
    payload: ComplaintCreate, db: Session = Depends(get_db),
    # S3.3 backend fix: also admits a Coordinator with a dual Teacher
    # assignment (see RoleSwitchService/teacherPortalGuard on the
    # frontend) submitting Teacher-style feedback while in Teacher mode.
    # No separate assignment lookup needed — the "teacher" branch below
    # (payload.student_id must be None) already applies identically to
    # both, and list_complaints' submitted_by filter for their own
    # submissions works the same regardless of which of the two roles
    # made the request.
    current_user: User = Depends(require_roles("student", "parent", "teacher", "coordinator")),
):
    if current_user.role == "student" and current_user.id != payload.student_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Students may only submit on their own behalf")
    if current_user.role == "parent":
        if not payload.student_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="student_id is required for a Parent submission")
        link = db.query(ParentStudentLink).filter(
            ParentStudentLink.parent_id == current_user.id, ParentStudentLink.student_id == payload.student_id,
            ParentStudentLink.deleted_at.is_(None),
        ).first()
        if not link:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not linked to this student")
    if current_user.role in ("teacher", "coordinator") and payload.student_id is not None:
        # A Teacher's feedback/complaint (Sub-Sprint 6.2) is general — about
        # a timetable clash, a policy question, etc — not about one student.
        # Reject rather than silently drop it, so the caller notices if it
        # meant to submit a Student/Parent-style complaint instead. Applies
        # to a dual-role Coordinator in Teacher mode exactly the same way —
        # their own Coordinator identity already has full visibility into
        # every complaint via list_complaints below, so there's no separate
        # "Coordinator complaint about a student" flow this could be for.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail="Teacher feedback isn't submitted on behalf of a specific student")

    complaint = Complaint(submitted_by=current_user.id, **payload.model_dump())
    db.add(complaint)
    db.commit()
    db.refresh(complaint)

    # Visible to both Coordinator and Admin (see list_complaints below) —
    # notify every active one, same "no single owner" pattern as mark edit
    # requests.
    reviewers = db.query(User).filter(User.role.in_(["coordinator", "admin"]), User.deleted_at.is_(None)).all()
    for reviewer in reviewers:
        notify(
            db, reviewer.id, "complaint_submitted",
            f"{current_user.full_name} submitted a complaint/feedback: {payload.subject_of_complaint or payload.description[:60]}",
            related_entity_type="complaints", related_entity_id=complaint.id,
        )
    db.commit()
    return _complaint_out(db, complaint)


@router.get("", response_model=List[ComplaintOut])
def list_complaints(db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    # Visible to BOTH Coordinator and Admin simultaneously, no routing split.
    query = db.query(Complaint).filter(Complaint.deleted_at.is_(None))
    if current_user.role in ("admin", "coordinator"):
        pass
    elif current_user.role == "student":
        query = query.filter(Complaint.student_id == current_user.id)
    elif current_user.role in ("parent", "teacher"):
        # Parent: complaints they filed on a child's behalf. Teacher: their
        # own submitted feedback (Sub-Sprint 6.2's status-tracking list) —
        # same shape, both keyed off who actually submitted it.
        query = query.filter(Complaint.submitted_by == current_user.id)
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
    complaints = query.order_by(Complaint.created_at.desc()).all()
    return [_complaint_out(db, c) for c in complaints]


@router.patch("/{complaint_id}", response_model=ComplaintOut)
def update_complaint_status(complaint_id: uuid.UUID, payload: ComplaintUpdate, db: Session = Depends(get_db),
                             current_user: User = Depends(require_roles("admin", "coordinator"))):
    complaint = db.query(Complaint).filter(Complaint.id == complaint_id, Complaint.deleted_at.is_(None)).first()
    if not complaint:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Complaint not found")
    if payload.status not in ("open", "in_progress", "resolved", "closed"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")

    complaint.status = payload.status
    if payload.resolution_message is not None:
        complaint.resolution_message = payload.resolution_message
    if payload.status in ("resolved", "closed"):
        complaint.resolved_by = current_user.id
        complaint.resolved_at = datetime.now(timezone.utc)

    # "submitter notified at each status change" — per the Complaint
    # Handling workflow doc. Wasn't wired up before this Sub-Sprint; every
    # status transition now fires one, carrying the reply text if given.
    submitter = db.query(User).filter(User.id == complaint.submitted_by).first()
    if submitter:
        message = f"Your complaint/feedback status changed to '{payload.status}'."
        if payload.resolution_message:
            message += f" {payload.resolution_message}"
        notify(
            db, submitter.id, "complaint_status_changed", message,
            related_entity_type="complaints", related_entity_id=complaint.id,
            email_to=submitter.email, email_subject="Your complaint/feedback was updated",
        )

    db.commit()
    db.refresh(complaint)
    return _complaint_out(db, complaint)
