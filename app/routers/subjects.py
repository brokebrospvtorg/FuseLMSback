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


@router.delete("/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subject(
    subject_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """Delete Subject — with dependency check. This is a soft delete
    (deleted_at + is_active=False, same convention as every other entity
    in this schema), and it's blocked entirely if anything real is tied
    to the subject: batch offerings, student enrollments, teacher
    assignments, or subject requests. Those rows carry FKs to subjects.id
    with no ON DELETE behavior defined, and more importantly represent
    real academic history (a student's enrollment record, a teacher's
    assignment) that a Delete click shouldn't silently orphan or hide.
    If any exist, the Admin is pointed at Deactivate instead — the
    reversible, non-destructive alternative above.
    """
    subject = _load_subject_or_404(db, subject_id)

    blockers: list[str] = []

    offering_count = (
        db.query(func.count(BatchSubject.id))
        .filter(BatchSubject.subject_id == subject_id)
        .scalar()
    )
    if offering_count:
        blockers.append(f"{offering_count} batch offering(s)")

    enrollment_count = (
        db.query(func.count(Enrollment.id))
        .filter(Enrollment.subject_id == subject_id, Enrollment.deleted_at.is_(None))
        .scalar()
    )
    if enrollment_count:
        blockers.append(f"{enrollment_count} active enrollment(s)")

    assignment_count = (
        db.query(func.count(TeacherSubjectAssignment.id))
        .filter(
            TeacherSubjectAssignment.subject_id == subject_id,
            TeacherSubjectAssignment.deleted_at.is_(None),
        )
        .scalar()
    )
    if assignment_count:
        blockers.append(f"{assignment_count} teacher assignment(s)")

    request_count = (
        db.query(func.count(SubjectRequest.id))
        .filter(SubjectRequest.subject_id == subject_id, SubjectRequest.deleted_at.is_(None))
        .scalar()
    )
    if request_count:
        blockers.append(f"{request_count} subject request(s)")

    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete '{subject.name}' — it's still referenced by "
                f"{', '.join(blockers)}. Deactivate it instead to hide it from "
                "new offerings while keeping this history intact."
            ),
        )

    old_value = {"name": subject.name, "code": subject.code, "is_active": subject.is_active}
    subject.deleted_at = datetime.now(timezone.utc)
    subject.is_active = False

    log_action(db, current_user.id, "subject_deleted", "subjects", subject.id, old_value, None)
    db.commit()

    return None
