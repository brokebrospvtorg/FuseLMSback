import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.models import Grade, Mark, Assessment, Subject, User, Level
from app.schemas.student_grades import (
    GradeReportEntryOut, SubjectMarksReportOut, MarkEntryOut,
)

# NOTE: this is deliberately a separate router from routers/marks.py (which
# stays mounted at /api/academics and handles teacher/admin assessment +
# grade management). This one is the read-only "me" surface the Angular
# MarksService calls, matching its baseUrl of /api/marks exactly.
router = APIRouter(prefix="/api/marks", tags=["student-marks"], dependencies=[Depends(check_license)])


@router.get("/me/grades", response_model=List[GradeReportEntryOut])
def my_grade_report(db: Session = Depends(get_db), current_user: User = Depends(require_roles("student"))):
    """One row per subject: the student's auto-computed (or overridden) grade.

    Note: whether the grade was coordinator-overridden is intentionally
    NOT surfaced here (see GradeReportEntryOut) — students only ever see
    the clean final percentage + letter grade, never override metadata.
    """
    rows = (
        db.query(Grade, Subject.name.label("subject_name"), Level.code.label("level_code"))
        .join(Subject, Subject.id == Grade.subject_id)
        .outerjoin(Level, Level.id == Subject.level_id)
        .filter(Grade.student_id == current_user.id, Grade.deleted_at.is_(None))
        .all()
    )
    return [
        GradeReportEntryOut(
            subject_id=grade.subject_id,
            subject_name=subject_name,
            level_code=level_code,
            computed_percentage=float(grade.computed_percentage) if grade.computed_percentage is not None else None,
            letter_grade=grade.letter_grade,
        )
        for grade, subject_name, level_code in rows
    ]


@router.get("/me/marks/{subject_id}", response_model=SubjectMarksReportOut)
def my_marks_for_subject(subject_id: uuid.UUID, db: Session = Depends(get_db),
                          current_user: User = Depends(require_roles("student"))):
    """Component-wise marks (quizzes, assignments, midterms, finals) for one subject."""
    subject = db.query(Subject).filter(Subject.id == subject_id, Subject.deleted_at.is_(None)).first()
    if not subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

    level = db.query(Level).filter(Level.id == subject.level_id).first() if subject.level_id else None

    rows = (
        db.query(Mark, Assessment.name.label("assessment_name"), Assessment.max_marks)
        .join(Assessment, Assessment.id == Mark.assessment_id)
        .filter(
            Mark.student_id == current_user.id,
            Assessment.subject_id == subject_id,
            Assessment.status == "published",  # marks stay hidden until the teacher publishes
            Mark.deleted_at.is_(None),
            Assessment.deleted_at.is_(None),
        )
        .all()
    )

    assessments = [
        MarkEntryOut(
            assessment_id=mark.assessment_id,
            assessment_name=assessment_name,
            max_marks=float(max_marks),
            marks_obtained=float(mark.marks_obtained),
        )
        for mark, assessment_name, max_marks in rows
    ]
    return SubjectMarksReportOut(
        subject_id=subject_id,
        subject_name=subject.name,
        level_code=level.code if level else None,
        assessments=assessments,
    )
