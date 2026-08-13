import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.audit import log_action
from app.core.notifications import notify
from app.models import (
    Batch, Level, Subject, StudentLevelEnrollment, SubjectRequest, Enrollment,
    TeacherSubjectAssignment, User, TimetableSlot, AttendanceRecord, Assessment, Mark,
)
from app.schemas.academic import (
    BatchCreate, BatchOut, LevelCreate, LevelOut, SubjectCreate, SubjectOut,
    StudentLevelEnrollmentCreate, StudentLevelEnrollmentOut,
    SubjectRequestCreate, SubjectRequestReview, SubjectRequestOut, EnrollmentOut,
    TeacherSubjectAssignmentCreate, TeacherSubjectAssignmentOut,
    TimetableEntryOut, DashboardSummaryOut, SubjectRequestReviewRowOut,
)

router = APIRouter(prefix="/api/academic", tags=["academic"], dependencies=[Depends(check_license)])


# ---------------------------------------------------------------------------
# Batches — Admin/Coordinator manage; only one is_current=true (DB-enforced)
# ---------------------------------------------------------------------------
@router.post("/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(require_roles("admin", "coordinator"))):
    if payload.is_current:
        db.query(Batch).filter(Batch.is_current.is_(True)).update({"is_current": False})
    batch = Batch(**payload.model_dump())
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=List[BatchOut])
def list_batches(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(Batch).filter(Batch.deleted_at.is_(None)).order_by(Batch.start_date.desc()).all()


@router.patch("/batches/{batch_id}/set-current", response_model=BatchOut)
def set_current_batch(batch_id: uuid.UUID, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("admin", "coordinator"))):
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    db.query(Batch).filter(Batch.is_current.is_(True)).update({"is_current": False})
    batch.is_current = True
    db.commit()
    db.refresh(batch)
    return batch


# ---------------------------------------------------------------------------
# Levels & Subjects — creatable by Admin or Coordinator
# ---------------------------------------------------------------------------
@router.post("/levels", response_model=LevelOut, status_code=status.HTTP_201_CREATED)
def create_level(payload: LevelCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(require_roles("admin", "coordinator"))):
    level = Level(**payload.model_dump())
    db.add(level)
    db.commit()
    db.refresh(level)
    return level


@router.get("/levels", response_model=List[LevelOut])
def list_levels(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return db.query(Level).filter(Level.deleted_at.is_(None)).order_by(Level.display_order).all()


@router.post("/subjects", response_model=SubjectOut, status_code=status.HTTP_201_CREATED)
def create_subject(payload: SubjectCreate, db: Session = Depends(get_db),
                    current_user: User = Depends(require_roles("admin", "coordinator"))):
    subject = Subject(**payload.model_dump())
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return subject


@router.get("/subjects", response_model=List[SubjectOut])
def list_subjects(level_id: Optional[uuid.UUID] = None, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    query = db.query(Subject).filter(Subject.deleted_at.is_(None))
    if level_id:
        query = query.filter(Subject.level_id == level_id)
    return query.order_by(Subject.name).all()


# ---------------------------------------------------------------------------
# Student level enrollments (O Level / AS Level / A2 Level)
# ---------------------------------------------------------------------------
@router.post("/level-enrollments", response_model=StudentLevelEnrollmentOut, status_code=status.HTTP_201_CREATED)
def create_level_enrollment(payload: StudentLevelEnrollmentCreate, db: Session = Depends(get_db),
                             current_user: User = Depends(require_roles("admin", "coordinator"))):
    # Business rule (procedural, not a DB constraint, to allow transfer/edge cases):
    # O Level must be 'completed' before an AS Level row can be created.
    level = db.query(Level).filter(Level.id == payload.level_id, Level.deleted_at.is_(None)).first()
    if not level:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Level not found")

    if level.name.lower().startswith("as"):
        o_level = db.query(Level).filter(Level.name.ilike("o level")).first()
        if o_level:
            completed_o_level = db.query(StudentLevelEnrollment).filter(
                StudentLevelEnrollment.student_id == payload.student_id,
                StudentLevelEnrollment.level_id == o_level.id,
                StudentLevelEnrollment.status == "completed",
                StudentLevelEnrollment.deleted_at.is_(None),
            ).first()
            if not completed_o_level:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Student must have a completed O Level enrollment before enrolling in AS Level",
                )

    enrollment = StudentLevelEnrollment(**payload.model_dump())
    db.add(enrollment)
    db.commit()
    db.refresh(enrollment)
    return enrollment


@router.get("/level-enrollments", response_model=List[StudentLevelEnrollmentOut])
def list_level_enrollments(student_id: Optional[uuid.UUID] = None, db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    query = db.query(StudentLevelEnrollment).filter(StudentLevelEnrollment.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(StudentLevelEnrollment.student_id == current_user.id)
    elif student_id:
        query = query.filter(StudentLevelEnrollment.student_id == student_id)
    return query.all()


# ---------------------------------------------------------------------------
# Subject requests -> approval auto-creates an enrollment row
# ---------------------------------------------------------------------------
@router.post("/subject-requests", response_model=SubjectRequestOut, status_code=status.HTTP_201_CREATED)
def create_subject_request(payload: SubjectRequestCreate, db: Session = Depends(get_db),
                            current_user: User = Depends(require_roles("student"))):
    req = SubjectRequest(student_id=current_user.id, **payload.model_dump())
    db.add(req)
    db.flush()  # assigns req.id before we reference it in notifications below

    # Notify everyone who can actually approve/reject this (Coordinators and
    # Admins both can, per the permission matrix) — no routing split, same
    # pattern as complaints being visible to both roles at once.
    subject = db.query(Subject).filter(Subject.id == req.subject_id).first()
    subject_name = subject.name if subject else "a subject"
    reviewers = db.query(User).filter(
        User.role.in_(("admin", "coordinator")), User.deleted_at.is_(None), User.status == "active",
    ).all()
    for reviewer in reviewers:
        notify(
            db, reviewer.id, "subject_request_submitted",
            f"{current_user.full_name} requested {subject_name}.",
            related_entity_type="subject_requests", related_entity_id=req.id,
        )

    db.commit()
    db.refresh(req)
    return req


@router.get("/subject-requests", response_model=List[SubjectRequestOut])
def list_subject_requests(status_filter: Optional[str] = None, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    query = db.query(SubjectRequest).filter(SubjectRequest.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(SubjectRequest.student_id == current_user.id)
    elif current_user.role not in ("admin", "coordinator"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
    if status_filter:
        query = query.filter(SubjectRequest.status == status_filter)
    return query.order_by(SubjectRequest.requested_at.desc()).all()


@router.get("/subject-requests/review-queue", response_model=List[SubjectRequestReviewRowOut])
def list_subject_request_review_queue(
    status_filter: Optional[str] = "requested",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Coordinator/Admin queue — defaults to just the pending ones (status_filter
    defaults to "requested", which is what's displayed as "Pending" in the UI;
    the enum itself is unchanged to avoid a migration). Pass status_filter=""
    or any specific value to see approved/rejected history too.
    """
    query = db.query(
        SubjectRequest, User.full_name.label("student_name"),
        Subject.name.label("subject_name"), Batch.name.label("batch_name"),
    ).join(
        User, User.id == SubjectRequest.student_id
    ).join(
        Subject, Subject.id == SubjectRequest.subject_id
    ).join(
        Batch, Batch.id == SubjectRequest.batch_id
    ).filter(SubjectRequest.deleted_at.is_(None))

    if status_filter:
        query = query.filter(SubjectRequest.status == status_filter)

    rows = query.order_by(SubjectRequest.requested_at.desc()).all()
    return [
        SubjectRequestReviewRowOut(
            id=req.id, student_id=req.student_id, student_name=student_name,
            subject_id=req.subject_id, subject_name=subject_name,
            batch_id=req.batch_id, batch_name=batch_name, status=req.status,
            requested_at=req.requested_at, actioned_by=req.actioned_by, actioned_at=req.actioned_at,
        )
        for req, student_name, subject_name, batch_name in rows
    ]


@router.patch("/subject-requests/{request_id}", response_model=SubjectRequestOut)
def review_subject_request(request_id: uuid.UUID, payload: SubjectRequestReview, db: Session = Depends(get_db),
                            current_user: User = Depends(require_roles("admin", "coordinator"))):
    req = db.query(SubjectRequest).filter(SubjectRequest.id == request_id, SubjectRequest.deleted_at.is_(None)).first()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject request not found")
    if req.status != "requested":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This request has already been actioned")
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be approved or rejected")

    req.status = payload.status
    req.actioned_by = current_user.id
    req.actioned_at = datetime.now(timezone.utc)

    if payload.status == "approved":
        existing = db.query(Enrollment).filter(
            Enrollment.student_id == req.student_id, Enrollment.subject_id == req.subject_id,
            Enrollment.batch_id == req.batch_id,
        ).first()
        if not existing:
            db.add(Enrollment(student_id=req.student_id, subject_id=req.subject_id, batch_id=req.batch_id))

    subject = db.query(Subject).filter(Subject.id == req.subject_id).first()
    subject_name = subject.name if subject else "the subject"
    verb = "approved" if payload.status == "approved" else "rejected"
    message = f"Your request for {subject_name} was {verb}."
    if payload.comment:
        message += f" Comment: {payload.comment}"
    notify(
        db, req.student_id, "subject_request_reviewed", message,
        related_entity_type="subject_requests", related_entity_id=req.id,
    )

    log_action(db, current_user.id, "subject_request_reviewed", "subject_requests", req.id, None,
               {"status": payload.status, "comment": payload.comment})
    db.commit()
    db.refresh(req)
    return req


@router.get("/enrollments", response_model=List[EnrollmentOut])
def list_enrollments(student_id: Optional[uuid.UUID] = None, subject_id: Optional[uuid.UUID] = None,
                      db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = db.query(Enrollment).filter(Enrollment.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(Enrollment.student_id == current_user.id)
    else:
        if student_id:
            query = query.filter(Enrollment.student_id == student_id)
    if subject_id:
        query = query.filter(Enrollment.subject_id == subject_id)
    return query.all()


# ---------------------------------------------------------------------------
# Teacher <-> Subject assignments (Coordinator by default, Admin also allowed)
# ---------------------------------------------------------------------------
@router.post("/teacher-assignments", response_model=TeacherSubjectAssignmentOut, status_code=status.HTTP_201_CREATED)
def assign_teacher(payload: TeacherSubjectAssignmentCreate, db: Session = Depends(get_db),
                    current_user: User = Depends(require_roles("admin", "coordinator"))):
    existing = db.query(TeacherSubjectAssignment).filter(
        TeacherSubjectAssignment.teacher_id == payload.teacher_id,
        TeacherSubjectAssignment.subject_id == payload.subject_id,
        TeacherSubjectAssignment.batch_id == payload.batch_id,
        TeacherSubjectAssignment.deleted_at.is_(None),
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Assignment already exists")

    assignment = TeacherSubjectAssignment(assigned_by=current_user.id, **payload.model_dump())
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment


@router.get("/teacher-assignments", response_model=List[TeacherSubjectAssignmentOut])
def list_teacher_assignments(teacher_id: Optional[uuid.UUID] = None, db: Session = Depends(get_db),
                              current_user: User = Depends(get_current_user)):
    query = db.query(TeacherSubjectAssignment).filter(TeacherSubjectAssignment.deleted_at.is_(None))
    if current_user.role == "teacher":
        query = query.filter(TeacherSubjectAssignment.teacher_id == current_user.id)
    elif teacher_id:
        query = query.filter(TeacherSubjectAssignment.teacher_id == teacher_id)
    return query.all()


# ---------------------------------------------------------------------------
# Student "me" endpoints — the Angular dashboard/timetable screens call these
# directly (see AcademicService.getMyTimetable / getDashboardSummary).
# ---------------------------------------------------------------------------
@router.get("/timetable/me", response_model=List[TimetableEntryOut])
def my_timetable_detailed(db: Session = Depends(get_db),
                           current_user: User = Depends(require_roles("student"))):
    """
    Same derived-view join as /api/timetable/my-timetable, but enriched with
    subject_name and teacher_name so the frontend grid doesn't need extra
    lookups per row.
    """
    subject_ids = [
        row.subject_id for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        ).all()
    ]
    if not subject_ids:
        return []

    rows = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), User.full_name.label("teacher_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(User, User.id == TimetableSlot.teacher_id)
        .filter(TimetableSlot.subject_id.in_(subject_ids), TimetableSlot.deleted_at.is_(None))
        .order_by(TimetableSlot.day_of_week, TimetableSlot.period_number)
        .all()
    )
    return [
        TimetableEntryOut(
            id=slot.id,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_name=teacher_name,
            day_of_week=slot.day_of_week,
            period_number=slot.period_number,
            start_time=slot.start_time,
            end_time=slot.end_time,
        )
        for slot, subject_name, teacher_name in rows
    ]


@router.get("/dashboard/summary", response_model=DashboardSummaryOut)
def dashboard_summary(db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("student"))):
    """
    High-level metadata for the student dashboard's top cards:
    overall attendance %, pending (unmarked, published) assessments,
    and current batch context.

    Note: FUSE LMS has no separate "assignment submission" concept in the
    schema — "pending assignments" is interpreted as published assessments
    in the student's active subjects that don't have a marks row yet.
    """
    current_batch = db.query(Batch).filter(Batch.is_current.is_(True), Batch.deleted_at.is_(None)).first()

    active_subject_ids = [
        row.subject_id for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        ).all()
    ]

    total_periods = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == current_user.id, AttendanceRecord.deleted_at.is_(None)
    ).count()
    attended_periods = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == current_user.id,
        AttendanceRecord.deleted_at.is_(None),
        AttendanceRecord.status.in_(["present", "late"]),
    ).count()
    attendance_percentage = round((attended_periods / total_periods) * 100, 1) if total_periods else 0.0

    pending_count = 0
    if active_subject_ids:
        published = db.query(Assessment).filter(
            Assessment.subject_id.in_(active_subject_ids),
            Assessment.status == "published",
            Assessment.deleted_at.is_(None),
        ).all()
        for assessment in published:
            has_mark = db.query(Mark).filter(
                Mark.assessment_id == assessment.id,
                Mark.student_id == current_user.id,
                Mark.deleted_at.is_(None),
            ).first()
            if not has_mark:
                pending_count += 1

    return DashboardSummaryOut(
        attendance_percentage=attendance_percentage,
        pending_assessments_count=pending_count,
        current_batch_name=current_batch.name if current_batch else None,
        current_batch_year=current_batch.year if current_batch else None,
        active_subjects_count=len(active_subject_ids),
    )
