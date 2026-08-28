"""
Admin Subjects module.

HISTORY: this filename previously held the schema_update_11-era "Subject &
Class Management" router (free-form, batch-scoped subject creation with a
required code, keyed off the old ClassSubject/class_subjects table). That
feature was superseded by the pre-declared Cambridge subject catalog
(app/models/academic.py:Subject, app/seeds/seed_subjects.py) and was left
unmounted — see app/main.py's note and app/models/subject.py for that old
model, which still exists only so historical class_subjects rows remain
queryable directly. This file has been repurposed for the Admin Subjects
screen and no longer touches ClassSubject at all.

SPLIT WITH app/routers/academic.py: GET/POST /api/academic/subjects
(list + create the catalog) stay in academic.py, since they're read/create
paths several non-admin screens depend on (Subject Requests, Teacher
Assignment, Offer Subjects) and were already working there. This module
adds the Admin-only mutations the Admin Subjects screen needs — edit,
activate/deactivate, delete — under the SAME /api/academic/subjects
prefix, on paths (/{id}, /{id}/status) that don't collide with those
existing routes. Both routers get mounted in main.py; FastAPI merges them
under one effective path tree.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.core.audit import log_action
from app.models import (
    Subject, SubjectLevel, Level, BatchSubject, Enrollment,
    TeacherSubjectAssignment, SubjectRequest, User,
    Assessment, Mark, MarkEditRequest, Grade,
    AttendanceRecord, TimetableSlot,
    Lecture, ClassroomEditRequest, YoutubeEditRequest,
    HelpingMaterial, SubjectClassroomLink,
    FeeStructure,
)
from app.schemas.academic import SubjectOut, SubjectUpdate, SubjectStatusUpdate

router = APIRouter(
    prefix="/api/academic/subjects", tags=["admin-subjects"],
    dependencies=[Depends(check_license)],
)


def _load_subject_or_404(db: Session, subject_id: uuid.UUID) -> Subject:
    subject = (
        db.query(Subject)
        .filter(Subject.id == subject_id, Subject.deleted_at.is_(None))
        .first()
    )
    if not subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")
    return subject


def _serialize(db: Session, subject: Subject) -> SubjectOut:
    """Same enrichment GET/POST /api/academic/subjects do in academic.py —
    duplicated rather than imported across router files to keep each
    router's read path independent of the other's internals."""
    levels = (
        db.query(Level)
        .join(SubjectLevel, SubjectLevel.level_id == Level.id)
        .filter(SubjectLevel.subject_id == subject.id)
        .order_by(Level.display_order)
        .all()
    )
    primary_level = next((lvl for lvl in levels if lvl.id == subject.level_id), None)
    if primary_level is None and subject.level_id:
        primary_level = db.query(Level).filter(Level.id == subject.level_id).first()

    return SubjectOut(
        id=subject.id, name=subject.name, code=subject.code, board=subject.board,
        is_active=subject.is_active, level_id=subject.level_id,
        level_name=primary_level.name if primary_level else None,
        levels=levels,
    )


@router.put("/{subject_id}", response_model=SubjectOut)
def update_subject(
    subject_id: uuid.UUID, payload: SubjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """Edit Subject Name/Code. Admin-only (Coordinator can still create via
    academic.py's POST, but renaming/re-coding an already-live catalog
    entry — which can ripple into every batch/enrollment/assignment that
    references it by name — is reserved for Admin on this screen).

    Duplicate validation is restricted to `code`, which is the unique
    catalog key — duplicate/similar names are allowed."""
    subject = _load_subject_or_404(db, subject_id)

    duplicate = (
        db.query(Subject)
        .filter(
            Subject.id != subject_id,
            Subject.deleted_at.is_(None),
            func.lower(Subject.code) == payload.code.lower(),
        )
        .first()
    )
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subject with this code already exists",
        )

    old_value = {"name": subject.name, "code": subject.code}
    subject.name = payload.name
    subject.code = payload.code

    log_action(
        db, current_user.id, "subject_updated", "subjects", subject.id,
        old_value, {"name": subject.name, "code": subject.code},
    )
    db.commit()
    db.refresh(subject)

    return _serialize(db, subject)


@router.patch("/{subject_id}/status", response_model=SubjectOut)
def set_subject_status(
    subject_id: uuid.UUID, payload: SubjectStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """Activate/Deactivate. Reversible, unlike delete: an inactive subject
    disappears from every "offer this subject" / enrollment / teacher-
    assignment picker (list_subjects in academic.py filters on
    is_active), but its history (past enrollments, marks, attendance tied
    to it) is untouched and it can be flipped back on at any time."""
    subject = _load_subject_or_404(db, subject_id)

    if subject.is_active == payload.is_active:
        return _serialize(db, subject)

    old_value = {"is_active": subject.is_active}
    subject.is_active = payload.is_active

    log_action(
        db, current_user.id,
        "subject_activated" if payload.is_active else "subject_deactivated",
        "subjects", subject.id, old_value, {"is_active": subject.is_active},
    )
    db.commit()
    db.refresh(subject)

    return _serialize(db, subject)


def _purge_subject_dependents(db: Session, subject_id: uuid.UUID) -> dict:
    """
    Hard-delete every row across the schema that references this subject,
    deepest children first, so the FK graph is clear before Subject itself
    is deleted. Runs inside the caller's transaction (no commit here) so
    the whole purge + the Subject delete succeed or fail together.

    Order matters — a child must be gone before its own parent-of-a-parent
    is removed, or Postgres will raise a FK violation:

      marks/mark_edit_requests -> assessments -> subjects
      attendance_records -> timetable_slots -> subjects
                          (attendance_records also FKs subjects directly)
      classroom_edit_requests/youtube_edit_requests -> lectures -> subjects
      helping_materials, subject_classroom_links -> subjects
      fee_structures -> subjects
      teacher_subject_assignments, enrollments, subject_requests,
      batch_subjects, subject_levels -> subjects

    Returns a per-table row count, written into the audit log so a hard
    delete like this still leaves a trail of exactly what it took with it.
    """
    purged: dict = {}

    # Marks & assessments
    mark_ids = [
        row.id for row in
        db.query(Mark.id)
        .join(Assessment, Assessment.id == Mark.assessment_id)
        .filter(Assessment.subject_id == subject_id)
        .all()
    ]
    purged["mark_edit_requests"] = (
        db.query(MarkEditRequest).filter(MarkEditRequest.mark_id.in_(mark_ids))
        .delete(synchronize_session=False) if mark_ids else 0
    )
    purged["marks"] = (
        db.query(Mark).filter(Mark.id.in_(mark_ids))
        .delete(synchronize_session=False) if mark_ids else 0
    )
    purged["assessments"] = (
        db.query(Assessment).filter(Assessment.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["grades"] = (
        db.query(Grade).filter(Grade.subject_id == subject_id)
        .delete(synchronize_session=False)
    )

    # Attendance & timetable (attendance_records FK both subjects and
    # timetable_slots, so it must go before timetable_slots is deleted)
    purged["attendance_records"] = (
        db.query(AttendanceRecord).filter(AttendanceRecord.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["timetable_slots"] = (
        db.query(TimetableSlot).filter(TimetableSlot.subject_id == subject_id)
        .delete(synchronize_session=False)
    )

    # Content: lectures, helping materials, classroom link
    lecture_ids = [
        row.id for row in db.query(Lecture.id).filter(Lecture.subject_id == subject_id).all()
    ]
    purged["classroom_edit_requests"] = (
        db.query(ClassroomEditRequest).filter(ClassroomEditRequest.lecture_id.in_(lecture_ids))
        .delete(synchronize_session=False) if lecture_ids else 0
    )
    purged["youtube_edit_requests"] = (
        db.query(YoutubeEditRequest).filter(YoutubeEditRequest.lecture_id.in_(lecture_ids))
        .delete(synchronize_session=False) if lecture_ids else 0
    )
    purged["lectures"] = (
        db.query(Lecture).filter(Lecture.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["helping_materials"] = (
        db.query(HelpingMaterial).filter(HelpingMaterial.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["subject_classroom_links"] = (
        db.query(SubjectClassroomLink).filter(SubjectClassroomLink.subject_id == subject_id)
        .delete(synchronize_session=False)
    )

    # Fees
    purged["fee_structures"] = (
        db.query(FeeStructure).filter(FeeStructure.subject_id == subject_id)
        .delete(synchronize_session=False)
    )

    # Core academic links: offerings, assignments, enrollments, requests
    purged["teacher_subject_assignments"] = (
        db.query(TeacherSubjectAssignment).filter(TeacherSubjectAssignment.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["enrollments"] = (
        db.query(Enrollment).filter(Enrollment.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["subject_requests"] = (
        db.query(SubjectRequest).filter(SubjectRequest.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["batch_subjects"] = (
        db.query(BatchSubject).filter(BatchSubject.subject_id == subject_id)
        .delete(synchronize_session=False)
    )
    purged["subject_levels"] = (
        db.query(SubjectLevel).filter(SubjectLevel.subject_id == subject_id)
        .delete(synchronize_session=False)
    )

    return purged


@router.delete("/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subject(
    subject_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """Delete Subject — PERMANENT CASCADE PURGE, no dependency check.

    Per product decision, this is no longer the reversible soft-delete it
    used to be, and no longer blocks on active references. On call, this
    hard-deletes the subject AND every row anywhere in the schema that
    points at it — batch offerings, enrollments, teacher assignments,
    subject requests, timetable slots, attendance records, assessments,
    marks (+ mark edit requests), grades, lectures (+ their edit
    requests), helping materials, the classroom link, and fee structures.
    See _purge_subject_dependents for the full list and the FK-safe order
    it runs in.

    THIS IS IRREVERSIBLE: student enrollment history, marks, attendance,
    and teacher assignment records tied to this subject are destroyed
    along with it, not archived. If that history needs to be preserved,
    use PATCH .../status (is_active=False) instead — this endpoint no
    longer offers a safety net.
    """
    subject = _load_subject_or_404(db, subject_id)
    old_value = {"name": subject.name, "code": subject.code, "is_active": subject.is_active}

    try:
        purged_counts = _purge_subject_dependents(db, subject_id)
        db.delete(subject)
        log_action(
            db, current_user.id, "subject_deleted", "subjects", subject.id,
            old_value, {"hard_deleted": True, "purged": purged_counts},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return None
