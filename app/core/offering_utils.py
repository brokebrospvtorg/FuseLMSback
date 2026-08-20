"""
Single source of truth for "is this batch+subject combo actually an
active offering right now" — schema_update_13/15's BatchSubject table is
where that question is meant to be answered (see BatchSubject's own
docstring in app/models/academic.py), but several write/read paths that
should have deferred to it were checking Batch/Subject existence only and
never asking BatchSubject at all:

  - POST /api/academic/teacher-assignments and
    POST /api/academic/batches/{batch_id}/assign-teacher could attach a
    Teacher to a subject+batch that had no active offering (or none at
    all) — nothing between "does the row exist" and "commit" ever
    consulted batch_subjects.
  - POST /api/timetable/slots (class_schedules) had the exact same gap on
    the scheduling side.

Once such a row exists, every "what can this teacher see" read path that
trusted it at face value (GET /academic/teacher-assignments, GET
/timetable/slots, GET /timetable/my-teaching-schedule) surfaced a
batch/board/subject the batch was never actually offering — the "empty
batch shows up", "all three boards show up" symptoms. This module is used
both to REJECT new assignments/slots that aren't backed by an active
offering, and to FILTER existing ones the same way when read back for a
Teacher.
"""
import uuid
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import BatchSubject, Subject


def active_boards_for(db: Session, batch_id: uuid.UUID, subject_id: uuid.UUID) -> list[str]:
    """Every board this subject is currently actively offered under, for
    this specific batch. Empty means "not offered here at all" (or the
    offering was withdrawn) — the only two states an Admin/Coordinator
    action or a Teacher-facing list should ever treat as "not usable"."""
    rows = (
        db.query(BatchSubject.board)
        .join(Subject, Subject.id == BatchSubject.subject_id)
        .filter(
            BatchSubject.batch_id == batch_id,
            BatchSubject.subject_id == subject_id,
            BatchSubject.is_active.is_(True),
            Subject.deleted_at.is_(None),
        )
        .distinct()
        .all()
    )
    return [row.board for row in rows]


def has_active_offering(db: Session, batch_id: uuid.UUID, subject_id: uuid.UUID) -> bool:
    return len(active_boards_for(db, batch_id, subject_id)) > 0


def active_boards_map(
    db: Session, pairs: Iterable[tuple[uuid.UUID, uuid.UUID]]
) -> dict[tuple[uuid.UUID, uuid.UUID], list[str]]:
    """Batch version of active_boards_for, for building N (assignment or
    slot) rows -> their active board(s) without one query per row."""
    pairs = list({p for p in pairs})
    if not pairs:
        return {}
    batch_ids = {p[0] for p in pairs}
    subject_ids = {p[1] for p in pairs}
    rows = (
        db.query(BatchSubject.batch_id, BatchSubject.subject_id, BatchSubject.board)
        .join(Subject, Subject.id == BatchSubject.subject_id)
        .filter(
            BatchSubject.batch_id.in_(batch_ids),
            BatchSubject.subject_id.in_(subject_ids),
            BatchSubject.is_active.is_(True),
            Subject.deleted_at.is_(None),
        )
        .distinct()
        .all()
    )
    result: dict[tuple[uuid.UUID, uuid.UUID], list[str]] = {}
    for batch_id, subject_id, board in rows:
        result.setdefault((batch_id, subject_id), []).append(board)
    return result
