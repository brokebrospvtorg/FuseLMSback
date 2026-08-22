import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, aliased

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.offering_utils import active_boards_for, active_boards_map
from app.core.security import guard_teacher_assignee_role
from app.models import TimetableSlot, Enrollment, User, Subject, Batch
from app.schemas.attendance import TimetableSlotCreate, TimetableSlotOut, TimetableSlotDetailOut, TimetableSlotUpdate
from app.schemas.common import BoardEnum

router = APIRouter(prefix="/api/timetable", tags=["timetable"], dependencies=[Depends(check_license)])


class TimetableSlotCreateCascading(TimetableSlotCreate):
    """
    Extends TimetableSlotCreate with `board` — the field the Coordinator's
    strict Batch -> Board -> Level -> Subject -> Teacher creation cascade
    actually selects third. board is NOT a TimetableSlot column (it's a
    batch_subjects concept — see BatchSubject's own docstring); requiring
    it here lets create_slot validate the Coordinator's actual selection is
    one of THIS batch+subject's active offering boards, instead of only
    checking that *some* board is active for the pair (which let a
    Coordinator pick a board the offering was never actually running
    under).
    """
    board: BoardEnum


def _find_teacher_time_conflict(
    db: Session, teacher_id: uuid.UUID, day_of_week, start_time, end_time,
    exclude_slot_id: Optional[uuid.UUID] = None,
):
    """
    schema_update_21: a teacher can't be scheduled for two overlapping
    periods on the same day — checked by actual clock time. Periods no
    longer carry a separate period_number label (schema_update_22 dropped
    the column); start_time/end_time is the only ordering and identity a
    slot has.

    Standard half-open interval overlap test: two ranges [s1, e1) and
    [s2, e2) overlap iff s1 < e2 AND s2 < e1. Catches an exact duplicate
    (exact same start/end) as a special case of this, not just as a
    separate equality check.
    """
    query = db.query(TimetableSlot).filter(
        TimetableSlot.teacher_id == teacher_id,
        TimetableSlot.day_of_week == day_of_week,
        TimetableSlot.deleted_at.is_(None),
        TimetableSlot.start_time < end_time,
        TimetableSlot.end_time > start_time,
    )
    if exclude_slot_id:
        query = query.filter(TimetableSlot.id != exclude_slot_id)
    return query.first()


@router.post("/slots", response_model=TimetableSlotOut, status_code=status.HTTP_201_CREATED)
def create_slot(payload: TimetableSlotCreateCascading, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Cascading Creation: enforces the full Batch -> Board -> Level -> Subject
    -> Teacher Assignee chain. batch_id/level_id/subject_id/teacher_id are
    already required fields on TimetableSlotCreate; `board` (added by
    TimetableSlotCreateCascading above) is validated against the batch+
    subject's real active offering board(s) rather than merely required to
    be present, since a syntactically valid-but-wrong board would defeat
    the point of the cascade.
    """
    # Admin Role Isolation Guard: a TimetableSlot.teacher_id must never
    # resolve to an admin/superadmin account — checked before anything
    # else touches the DB. See app/core/security.py::guard_teacher_assignee_role.
    teacher_user = db.query(User).filter(User.id == payload.teacher_id, User.deleted_at.is_(None)).first()
    if not teacher_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")
    guard_teacher_assignee_role(teacher_user.role, context="a Teacher on a Timetable Slot")

    active_boards = active_boards_for(db, payload.batch_id, payload.subject_id)
    if not active_boards:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This subject has no active offering for this batch. Offer the subject for this "
                   "batch (and board) before scheduling a class period for it.",
        )
    if payload.board.value not in active_boards:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{payload.board.value}' is not an active offering board for this batch/subject. "
                   f"Active board(s): {', '.join(sorted(active_boards))}.",
        )

    # schema_update_21: a teacher can't teach two overlapping periods at
    # once — reject up front with a clear message rather than letting the
    # request hit the DB-level unique index and surface as a raw,
    # confusing constraint-violation 500. Checked by actual start_time/
    # end_time overlap — see _find_teacher_time_conflict's docstring.
    if _find_teacher_time_conflict(db, payload.teacher_id, payload.day_of_week,
                                    payload.start_time, payload.end_time):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This teacher already has an overlapping period scheduled at that day/time.",
        )

    # `board` isn't a TimetableSlot column — it only exists on this payload
    # to be validated above, so it's excluded before constructing the row.
    slot = TimetableSlot(**payload.model_dump(exclude={"board"}))
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


@router.get("/slots", response_model=List[TimetableSlotDetailOut])
def list_slots(
    batch_id: Optional[uuid.UUID] = None,
    teacher_id: Optional[uuid.UUID] = None,
    board: Optional[BoardEnum] = None,
    level_id: Optional[uuid.UUID] = None,
    subject_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Coordinator Portal: upgraded from TimetableSlotOut (raw UUIDs only) to
    TimetableSlotDetailOut (joined subject/teacher/batch names) — the
    Interactive Timetable Builder's grid needs readable labels per cell,
    not IDs, same reason my-timetable and my-teaching-schedule already
    return the detailed shape. Results are always ordered strictly by
    `start_time` (no `period_number` anywhere in this codebase anymore —
    schema_update_22 dropped the column).

    Timetable search cascade (Batch -> Board -> Level -> Subject ->
    Teacher): level_id/subject_id/teacher_id/batch_id filter directly on
    TimetableSlot's own columns. `board` does NOT — it's a batch_subjects
    concept (see BatchSubject's docstring) — so it's applied via the same
    active-offering lookup already used for the Teacher-scoped branch's
    board field/fan-out below, now for every caller.

    Over-Inclusive Cascading Dropdowns fix: when this is a Teacher looking
    at their OWN schedule (the branch that feeds the Attendance screen's
    Day-Wise cascade), every slot is additionally cross-checked against an
    active batch_subjects offering and fanned out one row per active
    board — same treatment as GET /academic/teacher-assignments.
    Deliberately NOT applied to the Admin/Coordinator view of this same
    endpoint (no batch_id/teacher_id-driven Teacher scoping, or an
    explicit teacher_id lookup by an Admin/Coordinator) — the Interactive
    Timetable Builder needs to see every slot, including ones that have
    drifted out of sync with the offering, so there's something to click
    on and fix; a `board` filter is the one exception, since a Coordinator
    explicitly filtering by board only wants matches for that board.
    """
    teacher = aliased(User)
    query = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), teacher.full_name.label("teacher_name"),
                  Batch.name.label("batch_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(teacher, teacher.id == TimetableSlot.teacher_id)
        .join(Batch, Batch.id == TimetableSlot.batch_id)
        .filter(TimetableSlot.deleted_at.is_(None))
    )
    if batch_id:
        query = query.filter(TimetableSlot.batch_id == batch_id)
    if teacher_id:
        query = query.filter(TimetableSlot.teacher_id == teacher_id)
    if level_id:
        query = query.filter(TimetableSlot.level_id == level_id)
    if subject_id:
        query = query.filter(TimetableSlot.subject_id == subject_id)
    is_self_scoped_teacher = current_user.role == "teacher"
    if is_self_scoped_teacher:
        query = query.filter(TimetableSlot.teacher_id == current_user.id)
    # Strictly chronological — day_of_week groups the grid into columns,
    # start_time is the only in-day ordering. No period_number anywhere.
    rows = query.order_by(TimetableSlot.day_of_week, TimetableSlot.start_time).all()

    boards_by_pair = active_boards_map(db, ((slot.batch_id, slot.subject_id) for slot, _, _, _ in rows))

    if not is_self_scoped_teacher:
        result: List[TimetableSlotDetailOut] = []
        for slot, subject_name, teacher_name, batch_name in rows:
            if board and board.value not in boards_by_pair.get((slot.batch_id, slot.subject_id), []):
                continue
            result.append(TimetableSlotDetailOut(
                id=slot.id, subject_id=slot.subject_id, subject_name=subject_name,
                teacher_id=slot.teacher_id, teacher_name=teacher_name,
                batch_id=slot.batch_id, batch_name=batch_name,
                day_of_week=slot.day_of_week,
                start_time=slot.start_time, end_time=slot.end_time,
            ))
        return result

    result: List[TimetableSlotDetailOut] = []
    for slot, subject_name, teacher_name, batch_name in rows:
        boards = boards_by_pair.get((slot.batch_id, slot.subject_id), [])
        if not boards:
            continue  # no active offering left for this slot's batch+subject — still filtered out, same as before
        if board and board.value not in boards:
            continue
        # Bug fix: a slot used to be emitted once PER active board (a
        # subject offered under all 3 boards in the same batch rendered
        # the same 11:00 AM Monday period 3 times on the Teacher's weekly
        # grid). One physical period is one row, full stop. board is set
        # to whichever offering sorts first only so the field isn't null
        # on a Teacher-scoped read.
        result.append(TimetableSlotDetailOut(
            id=slot.id, subject_id=slot.subject_id, subject_name=subject_name,
            teacher_id=slot.teacher_id, teacher_name=teacher_name,
            batch_id=slot.batch_id, batch_name=batch_name,
            day_of_week=slot.day_of_week,
            start_time=slot.start_time, end_time=slot.end_time, board=sorted(boards)[0],
        ))
    return result


@router.patch("/slots/{slot_id}", response_model=TimetableSlotOut)
def update_slot(slot_id: uuid.UUID, payload: TimetableSlotUpdate, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Interactive Timetable Builder — Publish/Edit. PATCH in place (instead
    of delete+recreate) keeps the same row/id, so attendance_records
    already taken against this slot isn't orphaned.
    """
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id, TimetableSlot.deleted_at.is_(None)).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found")
    updates = payload.model_dump(exclude_unset=True)

    # Admin Role Isolation Guard: only re-checked when the edit actually
    # reassigns teacher_id, same "only re-check what actually changed"
    # pattern as the two guards below. See app/core/security.py.
    if "teacher_id" in updates:
        new_teacher = db.query(User).filter(
            User.id == updates["teacher_id"], User.deleted_at.is_(None)
        ).first()
        if not new_teacher:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")
        guard_teacher_assignee_role(new_teacher.role, context="a Teacher on a Timetable Slot")

    # Same active-offering guard as create — only re-checked when the
    # edit actually touches batch_id and/or subject_id, since those are
    # the two fields that determine which offering applies.
    if "batch_id" in updates or "subject_id" in updates:
        effective_batch_id = updates.get("batch_id", slot.batch_id)
        effective_subject_id = updates.get("subject_id", slot.subject_id)
        if not active_boards_for(db, effective_batch_id, effective_subject_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This subject has no active offering for this batch. Offer the subject for this "
                       "batch (and board) before moving a class period to it.",
            )

    # schema_update_21: same teacher-double-booked guard as create_slot,
    # checked by actual time overlap — only re-checked when the edit
    # actually touches teacher_id, day_of_week, start_time, and/or
    # end_time, since those four are what the conflict is keyed on.
    if any(f in updates for f in ("teacher_id", "day_of_week", "start_time", "end_time")):
        effective_teacher_id = updates.get("teacher_id", slot.teacher_id)
        effective_day = updates.get("day_of_week", slot.day_of_week)
        effective_start = updates.get("start_time", slot.start_time)
        effective_end = updates.get("end_time", slot.end_time)
        if _find_teacher_time_conflict(db, effective_teacher_id, effective_day,
                                        effective_start, effective_end, exclude_slot_id=slot.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This teacher already has an overlapping period scheduled at that day/time.",
            )

    for field, value in updates.items():
        setattr(slot, field, value)
    db.commit()
    db.refresh(slot)
    return slot


@router.get("/my-timetable", response_model=List[TimetableSlotDetailOut])
def my_timetable(db: Session = Depends(get_db), current_user: User = Depends(require_roles("student"))):
    """
    A student's personal timetable is a DERIVED VIEW: join enrollments +
    timetable_slots. Ordered strictly by start_time — no period_number.
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

    teacher = aliased(User)
    rows = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), teacher.full_name.label("teacher_name"),
                  Batch.name.label("batch_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(teacher, teacher.id == TimetableSlot.teacher_id)
        .join(Batch, Batch.id == TimetableSlot.batch_id)
        .filter(TimetableSlot.subject_id.in_(subject_ids), TimetableSlot.deleted_at.is_(None))
        .order_by(TimetableSlot.day_of_week, TimetableSlot.start_time)
        .all()
    )
    return [
        TimetableSlotDetailOut(
            id=slot.id,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_id=slot.teacher_id,
            teacher_name=teacher_name,
            batch_id=slot.batch_id,
            batch_name=batch_name,
            day_of_week=slot.day_of_week,
            start_time=slot.start_time,
            end_time=slot.end_time,
        )
        for slot, subject_name, teacher_name, batch_name in rows
    ]


_VALID_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


@router.get("/my-teaching-schedule", response_model=List[TimetableSlotDetailOut])
def my_teaching_schedule(
    day_of_week: Optional[str] = None,
    db: Session = Depends(get_db),
    # S3.3 backend fix: also admits a Coordinator with a dual Teacher
    # assignment. No separate assignment lookup needed — the query below
    # filters directly on TimetableSlot.teacher_id == current_user.id,
    # so a Coordinator only ever sees slots that are literally theirs.
    current_user: User = Depends(require_roles("teacher", "coordinator")),
):
    """
    A teacher's own weekly schedule — every slot where they're the
    assigned teacher_id, across all subjects/batches. Direct match on
    TimetableSlot.teacher_id since teachers aren't "enrolled" in anything.
    Strictly chronological by start_time within each day; no
    period_number anywhere in this codebase.

    day_of_week: optional filter for the Teacher Timetable screen's day
    tabs ("Monday".."Saturday", defaults to today client-side, "All Week"
    sends nothing).

    Over-Inclusive Cascading Dropdowns fix: same active-offering
    cross-check and per-board fan-out as the Teacher branch of GET
    /timetable/slots — this feeds the Teacher Portal's read-only Timetable
    screen and the Day-Wise Attendance cascade's final [Period/Date] step.
    """
    if day_of_week is not None and day_of_week.lower() not in _VALID_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid day_of_week '{day_of_week}'. Must be one of: {sorted(_VALID_DAYS)}",
        )

    query = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), Batch.name.label("batch_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(Batch, Batch.id == TimetableSlot.batch_id)
        .filter(TimetableSlot.teacher_id == current_user.id, TimetableSlot.deleted_at.is_(None))
    )
    if day_of_week is not None:
        query = query.filter(TimetableSlot.day_of_week == day_of_week.lower())

    rows = query.order_by(TimetableSlot.day_of_week, TimetableSlot.start_time).all()
    boards_by_pair = active_boards_map(db, ((slot.batch_id, slot.subject_id) for slot, _, _ in rows))
    result: List[TimetableSlotDetailOut] = []
    for slot, subject_name, batch_name in rows:
        boards = boards_by_pair.get((slot.batch_id, slot.subject_id), [])
        if not boards:
            continue  # no active offering left for this slot's batch+subject — still filtered out, same as before
        result.append(TimetableSlotDetailOut(
            id=slot.id,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_id=slot.teacher_id,
            teacher_name=current_user.full_name,
            batch_id=slot.batch_id,
            batch_name=batch_name,
            day_of_week=slot.day_of_week,
            start_time=slot.start_time,
            end_time=slot.end_time,
            board=sorted(boards)[0],
        ))
    return result


@router.delete("/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_slot(slot_id: uuid.UUID, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    from datetime import datetime, timezone
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id, TimetableSlot.deleted_at.is_(None)).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found")
    slot.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return None