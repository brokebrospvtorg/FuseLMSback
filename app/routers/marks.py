import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.audit import log_action
from app.core.notifications import notify
from app.models import Assessment, Mark, GradingScheme, Grade, User, Enrollment, StudentProfile, AuditLog
from app.schemas.marks import (
    AssessmentCreate, AssessmentOut, MarkUpsert, MarkOut,
    GradingSchemeCreate, GradingSchemeOut, GradeOverrideRequest, GradeOut, RosterEntryOut, AuditLogOut,
)
router = APIRouter(prefix="/api/academics", tags=["marks-grades"], dependencies=[Depends(check_license)])


# ---------------------------------------------------------------------------
# Assessments (Teacher creates; hidden from students until published)
# ---------------------------------------------------------------------------
@router.post("/assessments", response_model=AssessmentOut, status_code=status.HTTP_201_CREATED)
def create_assessment(payload: AssessmentCreate, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    assessment = Assessment(**payload.model_dump(), created_by=current_user.id)
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return assessment


@router.get("/assessments", response_model=List[AssessmentOut])
def list_assessments(subject_id: uuid.UUID, batch_id: uuid.UUID, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    query = db.query(Assessment).filter(
        Assessment.subject_id == subject_id, Assessment.batch_id == batch_id, Assessment.deleted_at.is_(None)
    )
    if current_user.role == "student":
        query = query.filter(Assessment.status == "published")
    return query.all()


@router.post("/assessments/{assessment_id}/publish", response_model=AssessmentOut)
def publish_assessment(assessment_id: uuid.UUID, db: Session = Depends(get_db),
                        current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    assessment = db.query(Assessment).filter(
        Assessment.id == assessment_id, Assessment.deleted_at.is_(None)
    ).first()
    if not assessment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")

    assessment.status = "published"
    db.commit()
    db.refresh(assessment)
    _recompute_grades_for_subject_batch(db, assessment.subject_id, assessment.batch_id, current_user.id)
    return assessment


# ---------------------------------------------------------------------------
# Roster — students actively enrolled in a subject+batch, for the Teacher's
# "enter marks" screen. Deliberately separate from GET /api/academic/enrollments
# (which doesn't return names) rather than expanding that response for every
# caller of it.
# ---------------------------------------------------------------------------
@router.get("/roster", response_model=List[RosterEntryOut])
def get_roster(subject_id: uuid.UUID, batch_id: uuid.UUID, db: Session = Depends(get_db),
                current_user: User = Depends(require_roles("teacher", "coordinator", "admin"))):
    rows = (
        db.query(User.id, User.full_name, StudentProfile.roll_number)
        .join(Enrollment, Enrollment.student_id == User.id)
        .outerjoin(StudentProfile, StudentProfile.user_id == User.id)
        .filter(
            Enrollment.subject_id == subject_id,
            Enrollment.batch_id == batch_id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
            User.deleted_at.is_(None),
        )
        .order_by(User.full_name)
        .all()
    )
    return [RosterEntryOut(student_id=r[0], full_name=r[1], roll_number=r[2]) for r in rows]


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------
@router.put("/assessments/{assessment_id}/marks", response_model=List[MarkOut])
def upsert_marks(assessment_id: uuid.UUID, payload: List[MarkUpsert], db: Session = Depends(get_db),
                  current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    assessment = db.query(Assessment).filter(
        Assessment.id == assessment_id, Assessment.deleted_at.is_(None)
    ).first()
    if not assessment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")

    results = []
    for item in payload:
        if item.marks_obtained > assessment.max_marks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"marks_obtained ({item.marks_obtained}) cannot exceed max_marks ({assessment.max_marks})",
            )
        mark = db.query(Mark).filter(
            Mark.assessment_id == assessment_id, Mark.student_id == item.student_id
        ).first()
        if mark:
            mark.marks_obtained = item.marks_obtained
            mark.uploaded_by = current_user.id
        else:
            mark = Mark(assessment_id=assessment_id, student_id=item.student_id,
                        marks_obtained=item.marks_obtained, uploaded_by=current_user.id)
            db.add(mark)
        results.append(mark)

    db.commit()
    for m in results:
        db.refresh(m)

    if assessment.status == "published":
        _recompute_grades_for_subject_batch(db, assessment.subject_id, assessment.batch_id, current_user.id)

    return results


@router.get("/assessments/{assessment_id}/marks", response_model=List[MarkOut])
def list_marks(assessment_id: uuid.UUID, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    query = db.query(Mark).filter(Mark.assessment_id == assessment_id, Mark.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(Mark.student_id == current_user.id)
    return query.all()


# ---------------------------------------------------------------------------
# Grading schemes (per level; Admin configures)
# ---------------------------------------------------------------------------
@router.post("/grading-schemes", response_model=GradingSchemeOut, status_code=status.HTTP_201_CREATED)
def create_grading_scheme(payload: GradingSchemeCreate, db: Session = Depends(get_db),
                           current_user: User = Depends(require_roles("admin"))):
    scheme = GradingScheme(**payload.model_dump())
    db.add(scheme)
    db.commit()
    db.refresh(scheme)
    return scheme


@router.get("/grading-schemes", response_model=List[GradingSchemeOut])
def list_grading_schemes(level_id: uuid.UUID, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    return db.query(GradingScheme).filter(
        GradingScheme.level_id == level_id, GradingScheme.deleted_at.is_(None)
    ).order_by(GradingScheme.min_percentage).all()


# ---------------------------------------------------------------------------
# Grades — auto-computed; Coordinator can override (audited + notifies teacher)
# ---------------------------------------------------------------------------
def _recompute_grades_for_subject_batch(db: Session, subject_id: uuid.UUID, batch_id: uuid.UUID, actor_id: uuid.UUID):
    """Recomputes every enrolled student's grade for this subject+batch, pooled across
    published assessments: (sum of marks obtained / sum of max marks) * 100. No weightage —
    every published assessment counts for exactly what it's marked out of."""
    assessments = db.query(Assessment).filter(
        Assessment.subject_id == subject_id, Assessment.batch_id == batch_id,
        Assessment.status == "published", Assessment.deleted_at.is_(None),
    ).all()
    if not assessments:
        return

    student_ids = [row.student_id for row in db.query(Enrollment.student_id).filter(
        Enrollment.subject_id == subject_id, Enrollment.batch_id == batch_id,
        Enrollment.status == "active", Enrollment.deleted_at.is_(None),
    ).all()]

    subject = db.query(Assessment).filter(Assessment.id == assessments[0].id).first()
    from app.models import Subject as SubjectModel
    subj = db.query(SubjectModel).filter(SubjectModel.id == subject_id).first()

    for student_id in student_ids:
        obtained_total = Decimal("0")
        max_total = Decimal("0")
        for a in assessments:
            mark = db.query(Mark).filter(
                Mark.assessment_id == a.id, Mark.student_id == student_id, Mark.deleted_at.is_(None)
            ).first()
            if mark and a.max_marks:
                obtained_total += mark.marks_obtained
                max_total += a.max_marks

        pooled_percentage = (obtained_total / max_total * Decimal("100")) if max_total else Decimal("0")

        letter_grade = None
        if subj:
            scheme_row = db.query(GradingScheme).filter(
                GradingScheme.level_id == subj.level_id,
                GradingScheme.min_percentage <= pooled_percentage,
                GradingScheme.max_percentage >= pooled_percentage,
                GradingScheme.deleted_at.is_(None),
            ).first()
            if scheme_row:
                letter_grade = scheme_row.letter_grade

        grade = db.query(Grade).filter(
            Grade.student_id == student_id, Grade.subject_id == subject_id, Grade.batch_id == batch_id
        ).first()
        if grade:
            if not grade.is_overridden:
                grade.computed_percentage = pooled_percentage
                grade.letter_grade = letter_grade
                grade.last_computed_at = datetime.now(timezone.utc)
        else:
            db.add(Grade(
                student_id=student_id, subject_id=subject_id, batch_id=batch_id,
                computed_percentage=pooled_percentage, letter_grade=letter_grade,
                last_computed_at=datetime.now(timezone.utc),
            ))
    db.commit()


@router.get("/grades", response_model=List[GradeOut])
def list_grades(student_id: Optional[uuid.UUID] = None, subject_id: Optional[uuid.UUID] = None,
                 batch_id: Optional[uuid.UUID] = None, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    query = db.query(Grade).filter(Grade.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(Grade.student_id == current_user.id)
    elif student_id:
        query = query.filter(Grade.student_id == student_id)
    if subject_id:
        query = query.filter(Grade.subject_id == subject_id)
    if batch_id:
        query = query.filter(Grade.batch_id == batch_id)
    return query.all()


@router.patch("/grades/{grade_id}/override", response_model=GradeOut)
def override_grade(grade_id: uuid.UUID, payload: GradeOverrideRequest, db: Session = Depends(get_db),
                    current_user: User = Depends(require_roles("coordinator", "admin"))):
    grade = db.query(Grade).filter(Grade.id == grade_id, Grade.deleted_at.is_(None)).first()
    if not grade:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grade not found")

    old_value = {"letter_grade": grade.letter_grade, "is_overridden": grade.is_overridden}
    grade.letter_grade = payload.letter_grade
    grade.is_overridden = True
    grade.overridden_by = current_user.id
    grade.override_reason = payload.override_reason

    log_action(db, current_user.id, "grade_overridden", "grades", grade.id, old_value,
               {"letter_grade": payload.letter_grade, "reason": payload.override_reason})

    # Notify both the original teacher and the student whose grade changed —
    # doc requires an in-app notification for each; email is a courtesy copy
    # via the (stub) send_email(), fired for both through the shared helper.
    original_assessment = db.query(Assessment).filter(
        Assessment.subject_id == grade.subject_id, Assessment.batch_id == grade.batch_id,
    ).first()
    if original_assessment:
        teacher = db.query(User).filter(User.id == original_assessment.created_by).first()
        if teacher:
            notify(
                db, teacher.id, "grade_overridden",
                f"A grade you submitted was overridden by a Coordinator: {payload.override_reason}",
                related_entity_type="grades", related_entity_id=grade.id,
                email_to=teacher.email, email_subject="Grade overridden",
            )

    student = db.query(User).filter(User.id == grade.student_id).first()
    if student:
        notify(
            db, student.id, "grade_overridden",
            f"Your grade was updated by a Coordinator to {payload.letter_grade}.",
            related_entity_type="grades", related_entity_id=grade.id,
            email_to=student.email, email_subject="Your grade was updated",
        )

    db.commit()
    db.refresh(grade)
    return grade


@router.get("/grades/audit-history", response_model=List[AuditLogOut])
def grade_override_audit_history(
    subject_id: uuid.UUID, batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("coordinator", "admin")),
):
    """
    Sub-Sprint 6.3: the "historical table of past overrides" for the
    Coordinator's Grade Override panel.

    Deliberately NOT just opening GET /api/audit-logs (audit.py) to
    Coordinator — that endpoint is Admin-only by design and returns every
    audit_logs row system-wide (role changes, fee approvals, everything).
    This is scoped to exactly what the panel needs: grade-override history
    for the subject+batch currently on screen, nothing else a Coordinator
    isn't supposed to see.
    """
    grade_ids = [row.id for row in db.query(Grade.id).filter(
        Grade.subject_id == subject_id, Grade.batch_id == batch_id,
    ).all()]
    if not grade_ids:
        return []

    return db.query(AuditLog).filter(
        AuditLog.entity_type == "grades",
        AuditLog.entity_id.in_(grade_ids),
    ).order_by(AuditLog.created_at.desc()).limit(200).all()
