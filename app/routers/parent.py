import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.models import (
    ParentStudentLink, StudentProfile, User, Batch, AttendanceRecord, Grade,
    Mark, Assessment, Subject,
)
from app.schemas.parent import ParentChildOut, ParentChildOverviewOut, ParentSubjectTranscriptOut, ParentMarkEntryOut

router = APIRouter(prefix="/api/parent", tags=["parent"], dependencies=[Depends(check_license)])


def _verify_linked_child(db: Session, parent_id: uuid.UUID, student_id: uuid.UUID) -> None:
    """
    The mandatory security rule from the spec: a Parent can only ever hit
    endpoints for their OWN linked student_id(s). Every endpoint below that
    takes a student_id calls this first — 403, not a filtered/empty result,
    if the link doesn't exist, so probing other students' IDs doesn't even
    leak "this id exists but isn't yours" via a 404 vs 403 distinction.
    """
    link = db.query(ParentStudentLink).filter(
        ParentStudentLink.parent_id == parent_id,
        ParentStudentLink.student_id == student_id,
        ParentStudentLink.deleted_at.is_(None),
    ).first()
    if not link:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This student is not linked to your account.",
        )


@router.get("/children", response_model=List[ParentChildOut])
def list_my_children(db: Session = Depends(get_db),
                      current_user: User = Depends(require_roles("parent"))):
    """Every child linked to the logged-in parent — feeds the Child Switcher widget."""
    rows = (
        db.query(ParentStudentLink, User, StudentProfile)
        .join(User, User.id == ParentStudentLink.student_id)
        .outerjoin(StudentProfile, StudentProfile.user_id == User.id)
        .filter(
            ParentStudentLink.parent_id == current_user.id,
            ParentStudentLink.deleted_at.is_(None),
            User.deleted_at.is_(None),
        )
        .order_by(User.full_name)
        .all()
    )
    return [
        ParentChildOut(
            student_id=user.id,
            full_name=user.full_name,
            roll_number=profile.roll_number if profile else None,
            relationship=link.relationship_label,
        )
        for link, user, profile in rows
    ]


@router.get("/child/{student_id}/overview", response_model=ParentChildOverviewOut)
def child_performance_overview(student_id: uuid.UUID, db: Session = Depends(get_db),
                                current_user: User = Depends(require_roles("parent"))):
    """
    The three metric cards: Current Batch, Overall Attendance %, Aggregate
    Grade. "Current Batch" is the school-wide is_current batch (batches.is_current
    is a single global flag, not per-student — see 001_init_schema.sql), not
    something specific to this child; still meaningful to show since it's
    the term everything else on the card is being measured against.
    """
    _verify_linked_child(db, current_user.id, student_id)

    student = db.query(User).filter(User.id == student_id, User.deleted_at.is_(None)).first()
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")

    current_batch = db.query(Batch).filter(Batch.is_current.is_(True), Batch.deleted_at.is_(None)).first()

    # Overall attendance % — same "late counts as attended" convention as
    # the student's own /api/attendance/me/summary, just not split by subject.
    att = (
        db.query(
            func.sum(case((AttendanceRecord.status.in_(["present", "late"]), 1), else_=0)).label("attended"),
            func.count(AttendanceRecord.id).label("total"),
        )
        .filter(AttendanceRecord.user_id == student_id, AttendanceRecord.deleted_at.is_(None))
        .first()
    )
    overall_attendance_pct = round((att.attended / att.total) * 100, 1) if att and att.total else None

    # Aggregate grade — plain average of computed_percentage across every
    # subject with a computed grade. A simple, defensible "aggregate"; not
    # weighted by subject credit-hours since nothing in the schema tracks those.
    avg_pct = (
        db.query(func.avg(Grade.computed_percentage))
        .filter(Grade.student_id == student_id, Grade.deleted_at.is_(None), Grade.computed_percentage.isnot(None))
        .scalar()
    )

    return ParentChildOverviewOut(
        student_id=student.id,
        full_name=student.full_name,
        current_batch_name=current_batch.name if current_batch else None,
        current_batch_year=current_batch.year if current_batch else None,
        overall_attendance_percentage=overall_attendance_pct,
        aggregate_grade_percentage=round(float(avg_pct), 1) if avg_pct is not None else None,
    )


@router.get("/child/{student_id}/report-card", response_model=List[ParentSubjectTranscriptOut])
def child_report_card(student_id: uuid.UUID, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("parent"))):
    """
    The Academic Transcript grid: every subject the child has a computed
    grade for, each with its assessment-by-assessment breakdown. Mirrors
    the logic in routers/student_grades.py's /me/grades + /me/marks/{id}
    (same 'only published assessments' privacy rule — a parent shouldn't
    see a mark before the student themselves would), just combined into
    one call and scoped to student_id instead of current_user.id.
    """
    _verify_linked_child(db, current_user.id, student_id)

    grade_rows = (
        db.query(Grade, Subject.name.label("subject_name"))
        .join(Subject, Subject.id == Grade.subject_id)
        .filter(Grade.student_id == student_id, Grade.deleted_at.is_(None))
        .all()
    )

    result = []
    for grade, subject_name in grade_rows:
        mark_rows = (
            db.query(Mark, Assessment.name.label("assessment_name"), Assessment.max_marks)
            .join(Assessment, Assessment.id == Mark.assessment_id)
            .filter(
                Mark.student_id == student_id,
                Assessment.subject_id == grade.subject_id,
                Assessment.status == "published",
                Mark.deleted_at.is_(None),
                Assessment.deleted_at.is_(None),
            )
            .all()
        )
        result.append(ParentSubjectTranscriptOut(
            subject_id=grade.subject_id,
            subject_name=subject_name,
            computed_percentage=float(grade.computed_percentage) if grade.computed_percentage is not None else None,
            letter_grade=grade.letter_grade,
            is_overridden=grade.is_overridden,
            assessments=[
                ParentMarkEntryOut(
                    assessment_id=mark.assessment_id,
                    assessment_name=assessment_name,
                    max_marks=float(max_marks),
                    marks_obtained=float(mark.marks_obtained),
                )
                for mark, assessment_name, max_marks in mark_rows
            ],
        ))
    return result
