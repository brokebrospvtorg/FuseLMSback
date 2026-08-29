"""
Single source of truth for "is this batch+subject combo actually an
active offering right now" — schema_update_13's BatchSubject table is
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
/timetable/slots, GET /timetable/my-teaching-schedule) surfaced a batch/
subject the batch was never actually offering. This module is used both
to REJECT new assignments/slots that aren't backed by an active offering,
and to FILTER existing ones the same way when read back for a Teacher.

Board removal: this module used to report which board(s) a batch+subject
offering was active under (`active_boards_for` / `active_boards_map`),
and callers fanned a single assignment/slot row out into one row per
active board. Now that BatchSubject has no `board` column, "is this an
active offering" collapses to a plain existence check — no more fan-out.
"""
import uuid
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import BatchSubject, Subject


def has_active_offering(db: Session, batch_id: uuid.UUID, subject_id: uuid.UUID) -> bool:
    """Whether this batch currently has an active (is_active=True,
    non-soft-deleted Subject) BatchSubject row for this subject — the
    only state a write path (assigning a teacher, scheduling a slot) or a
    Teacher-facing read should treat as "usable"."""
    row = (
        db.query(BatchSubject.id)
        .join(Subject, Subject.id == BatchSubject.subject_id)
        .filter(
            BatchSubject.batch_id == batch_id,
            BatchSubject.subject_id == subject_id,
            BatchSubject.is_active.is_(True),
            Subject.deleted_at.is_(None),
        )
        .first()
    )
    return row is not None


def active_offering_pairs(
    db: Session, pairs: Iterable[tuple[uuid.UUID, uuid.UUID]]
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Batch version of has_active_offering, for checking N (assignment or
    slot) rows -> whether each has an active offering, without one query
    per row. Returns the subset of the input pairs that DO have an active
    offering."""
    pairs = list({p for p in pairs})
    if not pairs:
        return set()
    batch_ids = {p[0] for p in pairs}
    subject_ids = {p[1] for p in pairs}
    rows = (
        db.query(BatchSubject.batch_id, BatchSubject.subject_id)
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
    found = {(batch_id, subject_id) for batch_id, subject_id in rows}
    return found & set(pairs)
