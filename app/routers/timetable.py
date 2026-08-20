import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, aliased

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.offering_utils import active_boards_for, active_boards_map
from app.models import TimetableSlot, Enrollment, User, Subject, Batch
from app.schemas.attendance import TimetableSlotCreate, TimetableSlotOut, TimetableSlotDetailOut, TimetableSlotUpdate

router = APIRouter(prefix="/api/timetable", tags=["timetable"], dependencies=[Depends(check_license)])


def _find_teacher_time_conflict(
    db: Session, teacher_id: uuid.UUID, day_of_week, start_time, end_time,
    exclude_slot_id: Optional[uuid.UUID] = None,
):
    """
    schema_update_21: a teacher can't be scheduled for two overlapping
    periods on the same day — checked by actual clock time, not
    period_number (period_number is a free-form label with no enforced
    relationship to start_time/end_time anywhere in this app, so keying a
    conflict check on it can miss real double-bookings entirely, which is
    exactly what happened before this fix).

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
def create_slot(payload: TimetableSlotCreate, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    # Over-Inclusive Cascading Dropdowns fix: a class_schedule (TimetableSlot)
    # must be backed by an actual active offering (batch_subjects), same
    # guard as POST /academic/teacher-assignments — otherwise this slot
    # shows up verbatim in the Teacher's Attendance cascade (Batch -> Board
    # -> Level -> Subject -> Period) for a batch/subject that was never
    # really offered, or whose offering has since been withdrawn.
    if not active_boards_for(db, payload.batch_id, payload.subject_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This subject has no active offering for this batch. Offer the subject for this "
                   "batch (and board) before scheduling a class period for it.",
        )

    # schema_update_21: a teacher can't teach two overlapping periods at
    # once — reject up front with a clear message rather than letting the
    # request hit the DB-level unique index
    # (idx_timetable_slots_teacher_day_time_active, exact-match only) and
    # surface as a raw, confusing constraint-violation 500. Checked by
    # actual start_time/end_time overlap, not period_number — see
    # _find_teacher_time_conflict's docstring for why period_number can't
    # be trusted for this. This is what actually fixes "the Teacher Portal
    # timetable shows the same period repeated 5 times": nothing before
    # this stopped a double form submission (or any other retry) from
    # inserting the same teacher/day/time more than once, each one filed
    # under a different period_number.
    if _find_teacher_time_conflict(db, payload.teacher_id, payload.day_of_week,
                                    payload.start_time, payload.end_time):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This teacher already has an overlapping period scheduled at that day/time.",
        )

    slot = TimetableSlot(**payload.model_dump())
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


@router.get("/slots", response_model=List[TimetableSlotDetailOut])
def list_slots(batch_id: Optional[uuid.UUID] = None, teacher_id: Optional[uuid.UUID] = None,
                db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Coordinator Portal Sub-Sprint 3: upgraded from TimetableSlotOut (raw
    UUIDs only) to TimetableSlotDetailOut (joined subject/teacher/batch
    names) — the Interactive Timetable Builder's grid needs readable labels
    per cell, not IDs, same reason my-timetable and my-teaching-schedule
    already return the detailed shape.

    Over-Inclusive Cascading Dropdowns fix: when this is a Teacher looking
    at their OWN schedule (the branch that feeds the Attendance screen's
    Batch -> Board -> Level -> Subject -> Period cascade), every slot is
    additionally cross-checked against an active batch_subjects offering
    and fanned out one row per active board — same treatment as GET
    /academic/teacher-assignments, and for the same reason (a raw
    TimetableSlot row doesn't mean the batch/subject is still a real,
    active offering). Deliberately NOT applied to the Admin/Coordinator
    view of this same endpoint (no batch_id/teacher_id-driven Teacher
    scoping, or an explicit teacher_id lookup by an Admin/Coordinator) —
    the Interactive Timetable Builder needs to see every slot, including
    ones that have drifted out of sync with the offering, so there's
    something to click on and fix.
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
    is_self_scoped_teacher = current_user.role == "teacher"
    if is_self_scoped_teacher:
        query = query.filter(TimetableSlot.teacher_id == current_user.id)
    rows = query.order_by(TimetableSlot.day_of_week, TimetableSlot.period_number).all()

    if not is_self_scoped_teacher:
        return [
            TimetableSlotDetailOut(
                id=slot.id, subject_id=slot.subject_id, subject_name=subject_name,
                teacher_id=slot.teacher_id, teacher_name=teacher_name,
                batch_id=slot.batch_id, batch_name=batch_name,
                day_of_week=slot.day_of_week, period_number=slot.period_number,
                start_time=slot.start_time, end_time=slot.end_time,
            )
            for slot, subject_name, teacher_name, batch_name in rows
        ]

    boards_by_pair = active_boards_map(db, ((slot.batch_id, slot.subject_id) for slot, _, _, _ in rows))
    result: List[TimetableSlotDetailOut] = []
    for slot, subject_name, teacher_name, batch_name in rows:
        boards = boards_by_pair.get((slot.batch_id, slot.subject_id), [])
        if not boards:
            continue  # no active offering left for this slot's batch+subject — still filtered out, same as before
        # Bug fix: a slot used to be emitted once PER active board (a
        # subject offered under all 3 boards in the same batch rendered
        # the same 11:00 AM Monday period 3 times on the Teacher's weekly
        # grid). board was only ever meant to disambiguate WHICH offering
        # backs a slot when that matters (it doesn't, here — this is a
        # single physical period on a single day at a single time); one
        # slot is one row, full stop. board is set to whichever offering
        # sorts first only so the field isn't null on a Teacher-scoped
        # read (per TimetableSlotDetailOut.board's own docstring) — no
        # code before or after this reads which one it picked.
        result.append(TimetableSlotDetailOut(
            id=slot.id, subject_id=slot.subject_id, subject_name=subject_name,
            teacher_id=slot.teacher_id, teacher_name=teacher_name,
            batch_id=slot.batch_id, batch_name=batch_name,
            day_of_week=slot.day_of_week, period_number=slot.period_number,
            start_time=slot.start_time, end_time=slot.end_time, board=sorted(boards)[0],
        ))
    return result


@router.patch("/slots/{slot_id}", response_model=TimetableSlotOut)
def update_slot(slot_id: uuid.UUID, payload: TimetableSlotUpdate, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Interactive Timetable Builder — Publish/Edit. This is the endpoint that
    was missing entirely (only create+delete existed): editing a slot in
    place instead of delete+recreate matters because delete+recreate would
    change slot_id, which attendance_records.timetable_slot_id references —
    any attendance already taken against the old slot would be silently
    orphaned from the "new" one. PATCH here keeps the same row/id.
    """
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id, TimetableSlot.deleted_at.is_(None)).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found")
    updates = payload.model_dump(exclude_unset=True)

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
    timetable_slots. Not stored as its own table. Joins Subject and the
    teacher's User row too, since the frontend slot-grid needs readable
    names, not raw UUIDs, in each cell.
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
        .order_by(TimetableSlot.day_of_week, TimetableSlot.period_number)
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
            period_number=slot.period_number,
            start_time=slot.start_time,
            end_time=slot.end_time,
        )
        for slot, subject_name, teacher_name, batch_name in rows
    ]


@router.get("/my-teaching-schedule", response_model=List[TimetableSlotDetailOut])
def my_teaching_schedule(db: Session = Depends(get_db),
                          current_user: User = Depends(require_roles("teacher"))):
    """
    A teacher's own weekly schedule — every slot where they're the
    assigned teacher_id, across all subjects/batches. Unlike a student's
    timetable (derived from enrollments), this is a direct match on
    TimetableSlot.teacher_id since teachers aren't "enrolled" in anything.

    Over-Inclusive Cascading Dropdowns fix: same active-offering
    cross-check and per-board fan-out as the Teacher branch of GET
    /timetable/slots — this feeds the Teacher Portal's read-only Timetable
    screen, so a period for a batch/subject with no active offering (or a
    withdrawn one) shouldn't appear on the weekly grid either.
    """
    rows = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), User.full_name.label("teacher_name"),
                  Batch.name.label("batch_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(Batch, Batch.id == TimetableSlot.batch_id)
        .filter(TimetableSlot.teacher_id == current_user.id, TimetableSlot.deleted_at.is_(None))
        .order_by(TimetableSlot.day_of_week, TimetableSlot.period_number)
        .all()
    )
    boards_by_pair = active_boards_map(db, ((slot.batch_id, slot.subject_id) for slot, _, _, _ in rows))
    result: List[TimetableSlotDetailOut] = []
    for slot, subject_name, _, batch_name in rows:
        boards = boards_by_pair.get((slot.batch_id, slot.subject_id), [])
        if not boards:
            continue  # no active offering left for this slot's batch+subject — still filtered out, same as before
        # Bug fix: same duplication as GET /timetable/slots' Teacher branch
        # above — a slot was emitted once PER active board, so a subject
        # offered under all 3 boards in the same batch showed the SAME
        # 11:00 AM Monday period 3 times in a row on this exact screen
        # (Teacher Portal > Timetable). One TimetableSlot is one period;
        # board only disambiguates which offering backs it when that's
        # actually relevant, which it isn't on a personal weekly grid. See
        # the longer version of this comment just above, in list_slots.
        result.append(TimetableSlotDetailOut(
            id=slot.id,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_id=slot.teacher_id,
            teacher_name=current_user.full_name,
            batch_id=slot.batch_id,
            batch_name=batch_name,
            day_of_week=slot.day_of_week,
            period_number=slot.period_number,
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
