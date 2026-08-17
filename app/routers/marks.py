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
from app.models import (
    Assessment, Mark, GradingScheme, Grade, User, Enrollment, StudentProfile, AuditLog,
    MarkEditRequest, Subject,
)
from app.schemas.marks import (
    AssessmentCreate, AssessmentUpdate, AssessmentOut, MarkUpsert, MarkOut,
    GradingSchemeCreate, GradingSchemeOut, GradeOverrideRequest, GradeOut, RosterEntryOut, AuditLogOut,
    MarkEditRequestCreate, MarkEditRequestReview, MarkEditRequestOut, MarkEditRequestWithContextOut,
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


@router.patch("/assessments/{assessment_id}", response_model=AssessmentOut)
def update_assessment(assessment_id: uuid.UUID, payload: AssessmentUpdate, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Coordinator/Admin direct edit — bypasses the Teacher lock (Teacher's
    only path to changing an assessment after creation is the
    mark-edit-request queue above; this is the "Coordinator can just fix
    it directly" escape hatch the spec calls for). Not exposed to
    Teacher — same restriction as delete, below.
    """
    assessment = db.query(Assessment).filter(
        Assessment.id == assessment_id, Assessment.deleted_at.is_(None)
    ).first()
    if not assessment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")

    if payload.max_marks is not None:
        # Don't silently invalidate marks that were valid against the old
        # ceiling — same rule upsert_marks enforces on the way in.
        highest_existing = (
            db.query(Mark)
            .filter(Mark.assessment_id == assessment_id, Mark.deleted_at.is_(None))
            .order_by(Mark.marks_obtained.desc())
            .first()
        )
        if highest_existing and payload.max_marks < highest_existing.marks_obtained:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot set max_marks below {highest_existing.marks_obtained} — a student already has that many marks recorded",
            )
        assessment.max_marks = payload.max_marks

    if payload.name is not None:
        assessment.name = payload.name

    db.commit()
    db.refresh(assessment)

    # max_marks changes the percentage every mark under it represents —
    # name changes don't, but recomputing on a plain rename is a cheap
    # no-op, not worth branching on.
    if assessment.status == "published":
        _recompute_grades_for_subject_batch(db, assessment.subject_id, assessment.batch_id, current_user.id)

    return assessment


@router.delete("/assessments/{assessment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assessment(assessment_id: uuid.UUID, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Coordinator/Admin direct delete — same "bypass the lock" rationale as
    update_assessment above. Cascades to the assessment's marks (soft
    delete, same as everywhere else) so a stale assessment_id can't be
    left pointing at live mark rows, then recomputes grades since removing
    an assessment changes every affected student's percentage.
    """
    assessment = db.query(Assessment).filter(
        Assessment.id == assessment_id, Assessment.deleted_at.is_(None)
    ).first()
    if not assessment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")

    now = datetime.now(timezone.utc)
    db.query(Mark).filter(Mark.assessment_id == assessment_id, Mark.deleted_at.is_(None)).update(
        {Mark.deleted_at: now}, synchronize_session=False
    )
    assessment.deleted_at = now
    db.commit()

    if assessment.status == "published":
        _recompute_grades_for_subject_batch(db, assessment.subject_id, assessment.batch_id, current_user.id)


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


@router.delete("/marks/{mark_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mark(mark_id: uuid.UUID, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    """Coordinator/Admin direct delete of one student's mark — same
    "bypass the Teacher lock" rationale as the assessment endpoints
    above. Not a full assessment delete: one wrong entry doesn't need
    to take the whole assessment down with it."""
    mark = db.query(Mark).filter(Mark.id == mark_id, Mark.deleted_at.is_(None)).first()
    if not mark:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mark not found")

    assessment = db.query(Assessment).filter(Assessment.id == mark.assessment_id).first()
    mark.deleted_at = datetime.now(timezone.utc)
    db.commit()

    if assessment and assessment.status == "published":
        _recompute_grades_for_subject_batch(db, assessment.subject_id, assessment.batch_id, current_user.id)


# ---------------------------------------------------------------------------
# Mark Edit Requests (Sub-Sprint 5) — marks are locked once saved (see
# upsert_marks above); a Teacher who needs to correct one raises a request
# here instead, and a Coordinator/Admin approves or rejects it. Same
# request -> review -> resolution shape as CorrectionRequest in users.py.
# ---------------------------------------------------------------------------
def _mark_edit_context_query(db: Session):
    """Base join used by both the 'mine' and 'pending' listings — enough to
    render a readable row without a second round-trip per request."""
    return (
        db.query(MarkEditRequest, Mark, Assessment, Subject, User)
        .join(Mark, Mark.id == MarkEditRequest.mark_id)
        .join(Assessment, Assessment.id == Mark.assessment_id)
        .join(Subject, Subject.id == Assessment.subject_id)
        .join(User, User.id == Mark.student_id)
        .filter(MarkEditRequest.deleted_at.is_(None))
    )


def _to_context_out(row) -> MarkEditRequestWithContextOut:
    mer, mark, assessment, subject, student = row
    return MarkEditRequestWithContextOut(
        id=mer.id, mark_id=mer.mark_id, requested_by=mer.requested_by,
        requested_change=mer.requested_change, reason=mer.reason, status=mer.status,
        reviewed_by=mer.reviewed_by, reviewed_at=mer.reviewed_at, created_at=mer.created_at,
        assessment_name=assessment.name, subject_name=subject.name, student_name=student.full_name,
        current_marks_obtained=mark.marks_obtained,
    )


@router.post("/marks/{mark_id}/edit-requests", response_model=MarkEditRequestOut, status_code=status.HTTP_201_CREATED)
def create_mark_edit_request(mark_id: uuid.UUID, payload: MarkEditRequestCreate, db: Session = Depends(get_db),
                              current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    mark = db.query(Mark).filter(Mark.id == mark_id, Mark.deleted_at.is_(None)).first()
    if not mark:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mark not found")
    if current_user.role == "teacher" and mark.uploaded_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                             detail="You may only request edits for marks you uploaded")

    existing = db.query(MarkEditRequest).filter(
        MarkEditRequest.mark_id == mark_id, MarkEditRequest.status == "pending",
        MarkEditRequest.deleted_at.is_(None),
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail="An edit request for this mark is already pending")

    if set(payload.requested_change.keys()) != {"marks_obtained"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail="requested_change must contain exactly one field: marks_obtained")
    new_marks = Decimal(str(payload.requested_change["marks_obtained"]))
    assessment = db.query(Assessment).filter(Assessment.id == mark.assessment_id).first()
    if new_marks < 0 or (assessment and new_marks > assessment.max_marks):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"marks_obtained must be between 0 and {assessment.max_marks if assessment else 'the assessment max'}",
        )

    mer = MarkEditRequest(
        mark_id=mark_id, requested_by=current_user.id,
        requested_change=payload.requested_change, reason=payload.reason,
    )
    db.add(mer)
    db.commit()
    db.refresh(mer)

    # Notify every active Coordinator/Admin — there's no single "owner" of
    # a request the way a Coordinator override notifies one specific Teacher.
    reviewers = db.query(User).filter(User.role.in_(["coordinator", "admin"]), User.deleted_at.is_(None)).all()
    for reviewer in reviewers:
        notify(
            db, reviewer.id, "mark_edit_requested",
            f"{current_user.full_name} requested a mark edit for {assessment.name if assessment else 'an assessment'}.",
            related_entity_type="mark_edit_requests", related_entity_id=mer.id,
        )
    db.commit()
    return mer


@router.get("/marks/edit-requests/mine", response_model=List[MarkEditRequestWithContextOut])
def list_my_mark_edit_requests(db: Session = Depends(get_db),
                                current_user: User = Depends(require_roles("teacher"))):
    rows = _mark_edit_context_query(db).filter(
        MarkEditRequest.requested_by == current_user.id
    ).order_by(MarkEditRequest.created_at.desc()).all()
    return [_to_context_out(row) for row in rows]


@router.get("/marks/edit-requests/pending", response_model=List[MarkEditRequestWithContextOut])
def list_pending_mark_edit_requests(db: Session = Depends(get_db),
                                     current_user: User = Depends(require_roles("coordinator", "admin"))):
    rows = _mark_edit_context_query(db).filter(
        MarkEditRequest.status == "pending"
    ).order_by(MarkEditRequest.created_at.asc()).all()
    return [_to_context_out(row) for row in rows]


@router.patch("/marks/edit-requests/{request_id}", response_model=MarkEditRequestOut)
def review_mark_edit_request(request_id: uuid.UUID, payload: MarkEditRequestReview, db: Session = Depends(get_db),
                              current_user: User = Depends(require_roles("coordinator", "admin"))):
    mer = db.query(MarkEditRequest).filter(
        MarkEditRequest.id == request_id, MarkEditRequest.deleted_at.is_(None)
    ).first()
    if not mer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mark edit request not found")
    if mer.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This request has already been reviewed")
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be approved or rejected")

    mer.status = payload.status
    mer.reviewed_by = current_user.id
    mer.reviewed_at = datetime.now(timezone.utc)
    mer.review_note = payload.review_note

    mark = db.query(Mark).filter(Mark.id == mer.mark_id, Mark.deleted_at.is_(None)).first()
    assessment = db.query(Assessment).filter(Assessment.id == mark.assessment_id).first() if mark else None

    if payload.status == "approved" and mark:
        old_value = {"marks_obtained": str(mark.marks_obtained)}
        for field, value in mer.requested_change.items():
            if hasattr(mark, field):
                setattr(mark, field, value)
        log_action(db, current_user.id, "mark_edit_approved", "marks", mark.id, old_value, mer.requested_change)
        if assessment and assessment.status == "published":
            _recompute_grades_for_subject_batch(db, assessment.subject_id, assessment.batch_id, current_user.id)

    requester = db.query(User).filter(User.id == mer.requested_by).first()
    if requester:
        message = (
            f"Your mark edit request for {assessment.name if assessment else 'an assessment'} was {payload.status}."
        )
        if payload.review_note:
            message += f" Note: {payload.review_note}"
        notify(
            db, requester.id, "mark_edit_reviewed", message,
            related_entity_type="mark_edit_requests", related_entity_id=mer.id,
            email_to=requester.email, email_subject="Mark edit request update",
        )

    db.commit()
    db.refresh(mer)
    return mer


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
