import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.models import Complaint, ParentStudentLink, User
from app.schemas.communication import ComplaintCreate, ComplaintUpdate, ComplaintOut

router = APIRouter(prefix="/api/complaints", tags=["complaints"], dependencies=[Depends(check_license)])


@router.post("", response_model=ComplaintOut, status_code=status.HTTP_201_CREATED)
def submit_complaint(payload: ComplaintCreate, db: Session = Depends(get_db),
                      current_user: User = Depends(require_roles("student", "parent"))):
    if current_user.role == "student" and current_user.id != payload.student_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Students may only submit on their own behalf")
    if current_user.role == "parent":
        link = db.query(ParentStudentLink).filter(
            ParentStudentLink.parent_id == current_user.id, ParentStudentLink.student_id == payload.student_id,
            ParentStudentLink.deleted_at.is_(None),
        ).first()
        if not link:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not linked to this student")

    complaint = Complaint(submitted_by=current_user.id, **payload.model_dump())
    db.add(complaint)
    db.commit()
    db.refresh(complaint)
    return complaint


@router.get("", response_model=List[ComplaintOut])
def list_complaints(db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    # Visible to BOTH Coordinator and Admin simultaneously, no routing split.
    query = db.query(Complaint).filter(Complaint.deleted_at.is_(None))
    if current_user.role in ("admin", "coordinator"):
        pass
    elif current_user.role == "student":
        query = query.filter(Complaint.student_id == current_user.id)
    elif current_user.role == "parent":
        query = query.filter(Complaint.submitted_by == current_user.id)
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
    return query.order_by(Complaint.created_at.desc()).all()


@router.patch("/{complaint_id}", response_model=ComplaintOut)
def update_complaint_status(complaint_id: uuid.UUID, payload: ComplaintUpdate, db: Session = Depends(get_db),
                             current_user: User = Depends(require_roles("admin", "coordinator"))):
    complaint = db.query(Complaint).filter(Complaint.id == complaint_id, Complaint.deleted_at.is_(None)).first()
    if not complaint:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Complaint not found")
    if payload.status not in ("open", "in_progress", "resolved"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status")

    complaint.status = payload.status
    if payload.status == "resolved":
        complaint.resolved_by = current_user.id
        complaint.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(complaint)
    return complaint
