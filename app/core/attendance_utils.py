import uuid
from datetime import date as date_type

from sqlalchemy import Row
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import AttendanceRecord

_TABLE = AttendanceRecord.__table__
_CONFLICT_TARGET = "uq_attendance_records_user_slot_date"  # matches models/attendance.py::__table_args__


def upsert_attendance_record(
    db: Session,
    *,
    user_id: uuid.UUID,
    subject_id: uuid.UUID,
    timetable_slot_id: uuid.UUID,
    date: date_type,
    status: str,
    marked_by: uuid.UUID,
    source: str,
) -> Row:
    """
    Atomically insert-or-update one attendance_records row keyed on the
    (user_id, timetable_slot_id, date) unique constraint.

    Replaces the old "SELECT ... ; if found UPDATE else INSERT" pattern used
    across every write path in routers/attendance.py. That pattern has a
    race window: two near-simultaneous requests for the same person +
    period + date can both run the SELECT, both see nothing, and both
    INSERT — which either duplicates the row (pre-constraint) or has one
    request fail with an unhandled IntegrityError (post-constraint). A
    single INSERT ... ON CONFLICT DO UPDATE resolves the conflict inside
    Postgres itself, in one round trip, with no window for a second
    request to interleave.

    Does not call db.commit() — callers commit once after all upserts in
    their request, same as before. Returns a Core Row with every column of
    the affected attendance_records row (via RETURNING), including
    server-generated `id` and `marked_at` on insert, or the pre-existing
    `marked_at` unchanged on update (matching the previous behavior, where
    an update never touched marked_at either). The Row supports attribute
    access, so it drops straight into AttendanceRecordOut / any schema with
    `Config.from_attributes = True` exactly like an ORM instance did.

    Void interaction: the ON CONFLICT target is the (user_id,
    timetable_slot_id, date) constraint, which is keyed on identity, not
    on deleted_at — so a row soft-deleted via DELETE /records/{id} is
    still exactly what a later re-mark for that same person/period/date
    conflicts against. The UPDATE explicitly clears deleted_at back to
    NULL so that re-mark revives the row instead of writing a fresh
    status onto a row that every read path in this router would still
    filter out as deleted.
    """
    stmt = (
        pg_insert(_TABLE)
        .values(
            user_id=user_id,
            subject_id=subject_id,
            timetable_slot_id=timetable_slot_id,
            date=date,
            status=status,
            marked_by=marked_by,
            source=source,
        )
        .on_conflict_do_update(
            constraint=_CONFLICT_TARGET,
            set_=dict(
                subject_id=subject_id,
                status=status,
                marked_by=marked_by,
                source=source,
                deleted_at=None,
            ),
        )
        .returning(_TABLE)
    )
    return db.execute(stmt).one()


def try_auto_mark_present(
    db: Session,
    *,
    user_id: uuid.UUID,
    subject_id: uuid.UUID,
    timetable_slot_id: uuid.UUID,
    date: date_type,
    marked_by: uuid.UUID,
) -> None:
    """
    Best-effort "mark present if nothing recorded yet" — used to auto-mark
    the teacher present when they submit their students' attendance for a
    period (see POST /mark-students). Must never overwrite a status that
    already exists (e.g. the teacher or a Coordinator already recorded
    them as absent/late) — same intent as the old
    "if not teacher_record: db.add(...)" check, just race-free.

    A plain ON CONFLICT DO NOTHING would treat a *voided* auto-mark row
    (deleted_at set via DELETE /records/{id}) the same as a live one and
    silently skip it forever, since the ON CONFLICT target is keyed on
    identity (user_id, timetable_slot_id, date), not on deleted_at — the
    teacher would then have no way to ever be auto-marked present again
    for that period. The conditional DO UPDATE below only fires when the
    existing row is currently voided (deleted_at IS NOT NULL); a live row
    (deleted_at IS NULL) fails that WHERE and is left untouched, so the
    "never overwrite an existing status" guarantee still holds exactly as
    before — this only closes the gap for a previously-voided row.
    """
    stmt = (
        pg_insert(_TABLE)
        .values(
            user_id=user_id,
            subject_id=subject_id,
            timetable_slot_id=timetable_slot_id,
            date=date,
            status="present",
            marked_by=marked_by,
            source="auto",
        )
        .on_conflict_do_update(
            constraint=_CONFLICT_TARGET,
            set_=dict(
                status="present",
                marked_by=marked_by,
                source="auto",
                deleted_at=None,
            ),
            where=_TABLE.c.deleted_at.isnot(None),
        )
    )
    db.execute(stmt)
