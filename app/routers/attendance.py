import uuid
from datetime import date as date_type, datetime, time, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session, aliased

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.offering_utils import active_offering_pairs
from app.core.audit import log_action
from app.core.notifications import notify
from app.core.attendance_utils import upsert_attendance_record, try_auto_mark_present
from app.models import AttendanceRecord, TimetableSlot, User, Subject, Enrollment, Level, Batch
from app.schemas.attendance import (
    StudentAttendanceMarkRequest, TeacherAttendanceOverrideRequest, AttendanceRecordOut,
    AttendanceRecordDetailOut, AttendanceSummaryOut, PeriodRecordOut,
    TeacherDailyLogRequest, TeacherDailyLogResult, TeacherDailyLogSkipped,
    TeacherRosterEntry, TeacherDailyStatusEntry,
    CoordinatorRosterEntry, CoordinatorStudentOverrideRequest,
    TeacherAttendanceLogEntry,
    AdminTeacherAttendanceEntry, AdminTeacherAttendanceMarkRequest,
)

router = APIRouter(prefix="/api/attendance", tags=["attendance"], dependencies=[Depends(check_license)])


class CoordinatorDayWiseEntry(BaseModel):
    """
    One row per period, for the Day-Wise Attendance View that replaces the
    old full-calendar Coordinator Attendance UI. Returned by the cascade
    endpoint below (Batch -> Level -> Subject -> Period/Date; Board removed)
    so the Coordinator can pick a period and jump into
    GET/POST /api/attendance/coordinator/roster + /override-students for
    that exact timetable_slot_id + date.
    """
    timetable_slot_id: uuid.UUID
    start_time: time
    end_time: time
    batch_id: uuid.UUID
    batch_name: str
    level_id: uuid.UUID
    level_code: Optional[str] = None
    subject_id: uuid.UUID
    subject_name: str
    teacher_id: uuid.UUID
    teacher_name: str
    present_count: int
    absent_count: int
    late_count: int
    excused_count: int
    total_students: int
    is_submitted: bool


@router.get("/coordinator/day-wise", response_model=List[CoordinatorDayWiseEntry])
def coordinator_day_wise_attendance(
    date: date_type,
    batch_id: uuid.UUID,
    level_id: Optional[uuid.UUID] = None,
    subject_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Day-Wise Attendance View (no calendar). `date` + `batch_id` are always
    required — nothing renders before a Batch and a day are picked, same
    as the frontend's cascade. `level_id`, `subject_id` progressively
    narrow that same day's periods, in that order: Batch -> Level ->
    Subject -> Period/Date. (Board was previously an extra step between
    Batch and Level; it's been removed along with the Board entity.)

    A slot's batch+subject still has to have an active offering
    (BatchSubject) — see has_active_offering's docstring — so this still
    checks that via the same active-offering lookup the Teacher-side
    timetable cascade uses, just without the board fan-out.

    Each row is one period on that day for the matching filters, with the
    student attendance already recorded against it for `date` (zeros +
    is_submitted=False when the teacher hasn't marked it yet) — the
    Coordinator drills into a specific row via the existing
    /coordinator/roster and /coordinator/override-students endpoints.
    """
    day_name = date.strftime("%A").lower()
    teacher = aliased(User)
    query = (
        db.query(
            TimetableSlot, Subject.name.label("subject_name"), Level.code.label("level_code"),
            teacher.full_name.label("teacher_name"), Batch.name.label("batch_name"),
        )
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(teacher, teacher.id == TimetableSlot.teacher_id)
        .join(Batch, Batch.id == TimetableSlot.batch_id)
        .outerjoin(Level, Level.id == TimetableSlot.level_id)
        .filter(
            TimetableSlot.batch_id == batch_id,
            TimetableSlot.day_of_week == day_name,
            TimetableSlot.deleted_at.is_(None),
        )
    )
    if level_id:
        query = query.filter(TimetableSlot.level_id == level_id)
    if subject_id:
        query = query.filter(TimetableSlot.subject_id == subject_id)
    # Strictly chronological — no period_number anywhere in this codebase.
    rows = query.order_by(TimetableSlot.start_time).all()
    if not rows:
        return []

    active_pairs = active_offering_pairs(db, ((slot.batch_id, slot.subject_id) for slot, *_ in rows))

    slot_ids = [slot.id for slot, *_ in rows]
    count_rows = (
        db.query(
            AttendanceRecord.timetable_slot_id,
            func.sum(case((AttendanceRecord.status == "present", 1), else_=0)).label("present_count"),
            func.sum(case((AttendanceRecord.status == "absent", 1), else_=0)).label("absent_count"),
            func.sum(case((AttendanceRecord.status == "late", 1), else_=0)).label("late_count"),
            func.sum(case((AttendanceRecord.status == "excused", 1), else_=0)).label("excused_count"),
            func.count(AttendanceRecord.id).label("total"),
        )
        # Explicit join (not just a bare filter reference) — TimetableSlot
        # has to actually be in the FROM clause for the teacher-exclusion
        # filter below to be a real join condition instead of an implicit
        # cross join against every slot.
        .join(TimetableSlot, TimetableSlot.id == AttendanceRecord.timetable_slot_id)
        .filter(
            AttendanceRecord.timetable_slot_id.in_(slot_ids),
            AttendanceRecord.user_id != TimetableSlot.teacher_id,  # student rows only, not the teacher's auto-mark
            AttendanceRecord.date == date,
            AttendanceRecord.deleted_at.is_(None),
        )
        .group_by(AttendanceRecord.timetable_slot_id)
        .all()
    )
    counts_by_slot = {c.timetable_slot_id: c for c in count_rows}

    result: List[CoordinatorDayWiseEntry] = []
    for slot, subject_name, level_code, teacher_name, batch_name in rows:
        if (slot.batch_id, slot.subject_id) not in active_pairs:
            continue  # no active offering left for this slot's batch+subject
        c = counts_by_slot.get(slot.id)
        result.append(CoordinatorDayWiseEntry(
            timetable_slot_id=slot.id,
            start_time=slot.start_time,
            end_time=slot.end_time,
            batch_id=slot.batch_id,
            batch_name=batch_name,
            level_id=slot.level_id,
            level_code=level_code,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_id=slot.teacher_id,
            teacher_name=teacher_name,
            present_count=c.present_count if c else 0,
            absent_count=c.absent_count if c else 0,
            late_count=c.late_count if c else 0,
            excused_count=c.excused_count if c else 0,
            total_students=c.total if c else 0,
            is_submitted=c is not None,
        ))
    return result


@router.post("/mark-students", response_model=List[AttendanceRecordOut], status_code=status.HTTP_201_CREATED)
def mark_student_attendance(
    payload: StudentAttendanceMarkRequest,
    db: Session = Depends(get_db),
    # S3.3 backend fix: also admits a Coordinator with a dual Teacher
    # assignment (see RoleSwitchService/teacherPortalGuard on the
    # frontend). No separate TeacherSubjectAssignment lookup is added
    # here — the ownership check immediately below (slot.teacher_id !=
    # current_user.id) already is the assignment check: TimetableSlot
    # .teacher_id is a direct FK to this specific user, so a Coordinator
    # can only ever pass this gate for a slot that is literally theirs
    # to teach, exactly the same as a real Teacher account.
    current_user: User = Depends(require_roles("teacher", "coordinator")),
):
    """
    Teacher marks per-period attendance for their own students.
    KEY LOGIC: marking students for a period auto-marks the teacher themselves
    'present' (source='auto') for that same period, unless already recorded.
    """
    slot = db.query(TimetableSlot).filter(
        TimetableSlot.id == payload.timetable_slot_id, TimetableSlot.deleted_at.is_(None)
    ).first()
    if not slot or slot.teacher_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your timetable slot")

    # subject_id is derivable from the slot itself — don't trust the client's
    # copy of it. A mismatch here would otherwise silently tag records to the
    # wrong subject even though timetable_slot_id points somewhere else.
    if payload.subject_id != slot.subject_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject_id does not match the subject assigned to this timetable slot.",
        )

    # Strict current-date enforcement: a Teacher may only create or edit
    # attendance (this endpoint upserts, so it covers both) for today.
    # Past dates are read-only for the Teacher; corrections to past dates
    # go through the Coordinator's override endpoints. Future dates aren't
    # markable at all.
    if payload.date != date_type.today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Teachers can only mark or edit attendance for the current date.",
        )

    # Every submitted student must be actively enrolled in this slot's
    # subject/batch — otherwise a teacher could submit a status for an
    # arbitrary user_id that was never on their roster to begin with.
    enrolled_ids = {
        row.student_id
        for row in db.query(Enrollment.student_id).filter(
            Enrollment.subject_id == payload.subject_id,
            Enrollment.batch_id == slot.batch_id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        ).all()
    }
    not_enrolled = [
        str(item.student_user_id) for item in payload.records
        if item.student_user_id not in enrolled_ids
    ]
    if not_enrolled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Student(s) not actively enrolled in this subject/batch: {', '.join(not_enrolled)}",
        )

    created = []
    for item in payload.records:
        row = upsert_attendance_record(
            db,
            user_id=item.student_user_id,
            subject_id=payload.subject_id,
            timetable_slot_id=payload.timetable_slot_id,
            date=payload.date,
            status=item.status,
            marked_by=current_user.id,
            source="manual",
        )
        created.append(row)

    # Auto-mark the teacher present for this period, if not already recorded.
    # ON CONFLICT DO NOTHING — never overwrites a status already present.
    try_auto_mark_present(
        db,
        user_id=current_user.id,
        subject_id=payload.subject_id,
        timetable_slot_id=payload.timetable_slot_id,
        date=payload.date,
        marked_by=current_user.id,
    )

    db.commit()
    return created


@router.get("/my-period-records", response_model=List[PeriodRecordOut])
def get_my_period_records(
    timetable_slot_id: uuid.UUID,
    date: date_type,
    db: Session = Depends(get_db),
    # S3.3 backend fix: also admits a Coordinator with a dual Teacher
    # assignment (see RoleSwitchService/teacherPortalGuard on the
    # frontend). No separate TeacherSubjectAssignment lookup is added
    # here — the ownership check immediately below (slot.teacher_id !=
    # current_user.id) already is the assignment check: TimetableSlot
    # .teacher_id is a direct FK to this specific user, so a Coordinator
    # can only ever pass this gate for a slot that is literally theirs
    # to teach, exactly the same as a real Teacher account.
    current_user: User = Depends(require_roles("teacher", "coordinator")),
):
    """
    Lets the Teacher's Mark Attendance screen check whether a period+date
    was already submitted (so it can render a locked, read-only view
    instead of the editable grid), and lets a past date be inspected
    read-only. Excludes the teacher's own auto-marked row.
    """
    slot = db.query(TimetableSlot).filter(
        TimetableSlot.id == timetable_slot_id, TimetableSlot.deleted_at.is_(None)
    ).first()
    if not slot or slot.teacher_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your timetable slot")

    records = db.query(AttendanceRecord).filter(
        AttendanceRecord.timetable_slot_id == timetable_slot_id,
        AttendanceRecord.date == date,
        AttendanceRecord.user_id != current_user.id,
        AttendanceRecord.deleted_at.is_(None),
    ).all()
    return [
        PeriodRecordOut(student_user_id=r.user_id, status=r.status, marked_at=r.marked_at)
        for r in records
    ]


@router.get("/my-history-log", response_model=List[TeacherAttendanceLogEntry])
def get_my_attendance_history_log(
    subject_id: Optional[uuid.UUID] = None,
    level_id: Optional[uuid.UUID] = None,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    db: Session = Depends(get_db),
    # S3.3 backend fix: also admits a Coordinator with a dual Teacher
    # assignment (see RoleSwitchService/teacherPortalGuard on the
    # frontend). No separate TeacherSubjectAssignment lookup is added
    # here — the ownership check immediately below (slot.teacher_id !=
    # current_user.id) already is the assignment check: TimetableSlot
    # .teacher_id is a direct FK to this specific user, so a Coordinator
    # can only ever pass this gate for a slot that is literally theirs
    # to teach, exactly the same as a real Teacher account.
    current_user: User = Depends(require_roles("teacher", "coordinator")),
):
    """
    Day-Wise UI's "View Summary" — a read-only historical log of classes
    this teacher has already taken (today or earlier only). One row per
    period+date actually taught, ordered strictly by start_time.
    """
    today = date_type.today()
    effective_date_to = min(date_to, today) if date_to else today

    query = (
        db.query(
            AttendanceRecord.date,
            AttendanceRecord.timetable_slot_id,
            TimetableSlot.start_time,
            AttendanceRecord.subject_id,
            Subject.name.label("subject_name"),
            Level.code.label("level_code"),
            func.sum(case((AttendanceRecord.status == "present", 1), else_=0)).label("present_count"),
            func.sum(case((AttendanceRecord.status == "absent", 1), else_=0)).label("absent_count"),
            func.sum(case((AttendanceRecord.status == "late", 1), else_=0)).label("late_count"),
            func.sum(case((AttendanceRecord.status == "excused", 1), else_=0)).label("excused_count"),
            func.count(AttendanceRecord.id).label("total_students"),
        )
        .join(TimetableSlot, TimetableSlot.id == AttendanceRecord.timetable_slot_id)
        .join(Subject, Subject.id == AttendanceRecord.subject_id)
        .outerjoin(Level, Level.id == TimetableSlot.level_id)
        .filter(
            TimetableSlot.teacher_id == current_user.id,
            AttendanceRecord.user_id != current_user.id,  # student rows only, not the teacher's own auto-mark
            AttendanceRecord.deleted_at.is_(None),
            AttendanceRecord.date <= effective_date_to,
        )
    )
    if subject_id:
        query = query.filter(AttendanceRecord.subject_id == subject_id)
    if level_id:
        query = query.filter(TimetableSlot.level_id == level_id)
    if date_from:
        query = query.filter(AttendanceRecord.date >= date_from)

    rows = (
        query.group_by(
            AttendanceRecord.date, AttendanceRecord.timetable_slot_id, TimetableSlot.start_time,
            AttendanceRecord.subject_id, Subject.name, Level.code,
        )
        .order_by(AttendanceRecord.date.desc(), TimetableSlot.start_time)
        .all()
    )

    return [
        TeacherAttendanceLogEntry(
            date=r.date,
            timetable_slot_id=r.timetable_slot_id,
            start_time=r.start_time,
            subject_id=r.subject_id,
            subject_name=r.subject_name,
            level_code=r.level_code,
            present_count=r.present_count,
            absent_count=r.absent_count,
            late_count=r.late_count,
            excused_count=r.excused_count,
            total_students=r.total_students,
        )
        for r in rows
    ]


@router.get("/teacher-gaps", response_model=List[dict])
def teacher_periods_without_records(
    date: date_type,
    batch_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Coordinator's screen: periods with NO existing attendance record at all
    (i.e., the teacher never logged in to mark their students).
    """
    day_name = date.strftime("%A").lower()
    query = db.query(TimetableSlot).filter(
        TimetableSlot.day_of_week == day_name, TimetableSlot.deleted_at.is_(None)
    )
    if batch_id:
        query = query.filter(TimetableSlot.batch_id == batch_id)
    slots = query.order_by(TimetableSlot.start_time).all()

    gaps = []
    for slot in slots:
        exists = db.query(AttendanceRecord).filter(
            AttendanceRecord.timetable_slot_id == slot.id, AttendanceRecord.date == date
        ).first()
        if not exists:
            gaps.append({
                "timetable_slot_id": str(slot.id),
                "teacher_id": str(slot.teacher_id),
                "subject_id": str(slot.subject_id),
                "start_time": str(slot.start_time),
                "end_time": str(slot.end_time),
            })
    return gaps


@router.get("/coordinator/roster", response_model=List[CoordinatorRosterEntry])
def coordinator_student_roster(
    timetable_slot_id: uuid.UUID,
    date: date_type,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Coordinator Portal: the Teacher's own roster fetch (`/my-period-records`)
    is locked to `slot.teacher_id == current_user.id` — this is the
    Coordinator equivalent, bypassing the teacher lock, for either a slot
    the teacher skipped entirely or one they already submitted. Every
    enrolled student for the slot's subject+batch, with whatever attendance
    status already exists for this date (None if nothing recorded yet).
    Reached from a row in GET /coordinator/day-wise.
    """
    slot = db.query(TimetableSlot).filter(
        TimetableSlot.id == timetable_slot_id, TimetableSlot.deleted_at.is_(None)
    ).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timetable slot not found")

    students = (
        db.query(User)
        .join(Enrollment, Enrollment.student_id == User.id)
        .filter(
            Enrollment.subject_id == slot.subject_id,
            Enrollment.batch_id == slot.batch_id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
            User.deleted_at.is_(None),
        )
        .order_by(User.full_name)
        .all()
    )
    existing = {
        r.user_id: r.status
        for r in db.query(AttendanceRecord).filter(
            AttendanceRecord.timetable_slot_id == timetable_slot_id,
            AttendanceRecord.date == date,
            AttendanceRecord.deleted_at.is_(None),
        ).all()
    }
    return [
        CoordinatorRosterEntry(student_user_id=s.id, full_name=s.full_name, status=existing.get(s.id))
        for s in students
    ]


@router.post("/coordinator/override-students", response_model=List[AttendanceRecordOut])
def coordinator_override_student_attendance(
    payload: CoordinatorStudentOverrideRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    The write half of the roster bypass, scoped to Admin/Coordinator only,
    for any date past or present. Deliberately does NOT touch the
    teacher's own auto-marked attendance row for this slot — that's edited
    separately via POST /api/attendance/teacher-override.
    """
    slot = db.query(TimetableSlot).filter(
        TimetableSlot.id == payload.timetable_slot_id, TimetableSlot.deleted_at.is_(None)
    ).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timetable slot not found")

    # subject_id is derivable from the slot itself — don't trust the client's
    # copy of it, same rule as POST /mark-students.
    if payload.subject_id != slot.subject_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject_id does not match the subject assigned to this timetable slot.",
        )

    saved = []
    for item in payload.records:
        row = upsert_attendance_record(
            db,
            user_id=item.student_user_id,
            subject_id=payload.subject_id,
            timetable_slot_id=payload.timetable_slot_id,
            date=payload.date,
            status=item.status,
            marked_by=current_user.id,
            source="manual",
        )
        saved.append(row)

    db.commit()
    return saved


@router.post("/teacher-override", response_model=AttendanceRecordOut, status_code=status.HTTP_201_CREATED)
def coordinator_mark_or_override_teacher(
    payload: TeacherAttendanceOverrideRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("coordinator", "admin")),
):
    """
    Coordinator/Admin manually marks (absent/late/excused) or overrides a teacher's
    attendance for a period. Mirrors POST /admin/teacher-attendance's integrity and
    accountability guarantees: the slot must exist and not be soft-deleted, the
    teacher must actually be the one assigned to it, and any change is written to
    the audit_logs trail (log_action) plus, on an override of an existing status,
    surfaced to the teacher via notify() — exactly like /admin/teacher-attendance
    does, so the two endpoints no longer diverge on accountability for the same
    conceptual action.
    """
    slot = db.query(TimetableSlot).filter(
        TimetableSlot.id == payload.timetable_slot_id, TimetableSlot.deleted_at.is_(None)
    ).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timetable slot not found")
    if slot.teacher_id != payload.teacher_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="teacher_user_id does not match the teacher assigned to this timetable slot.",
        )
    if payload.subject_id != slot.subject_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject_id does not match the subject assigned to this timetable slot.",
        )

    # A lightweight pre-check to decide the audit action / old_value / whether
    # this counts as an override for notification purposes. The actual write
    # below is still a single atomic upsert — this SELECT deciding "is this a
    # first mark or an override" can theoretically race against a second
    # concurrent request the same way it always could, but that only risks
    # mislabeling an audit action, never a duplicate row: the DB-level
    # uniqueness constraint + ON CONFLICT guarantee data integrity regardless
    # of what this pre-check believed.
    existing = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == payload.teacher_user_id,
        AttendanceRecord.timetable_slot_id == payload.timetable_slot_id,
        AttendanceRecord.date == payload.date,
    ).first()
    old_value = {"status": existing.status, "source": existing.source} if existing else None
    action = "teacher_attendance_overridden" if existing else "teacher_attendance_marked"

    record = upsert_attendance_record(
        db,
        user_id=payload.teacher_user_id,
        subject_id=payload.subject_id,
        timetable_slot_id=payload.timetable_slot_id,
        date=payload.date,
        status=payload.status,
        marked_by=current_user.id,
        source="manual",
    )

    log_action(
        db, current_user.id, action, "attendance_records", record.id, old_value,
        {"status": payload.status, "reason": payload.reason},
    )

    if action == "teacher_attendance_overridden":
        teacher = db.query(User).filter(User.id == payload.teacher_user_id).first()
        if teacher:
            reason_suffix = f": {payload.reason}" if payload.reason else ""
            actor_label = current_user.role.capitalize() if current_user.role else "Coordinator"
            notify(
                db, teacher.id, "teacher_attendance_overridden",
                f"Your attendance for {payload.date.isoformat()} was changed to "
                f"'{payload.status}' by a {actor_label}{reason_suffix}.",
                related_entity_type="attendance_records", related_entity_id=record.id,
            )

    db.commit()
    return record


@router.get("/admin/teacher-attendance", response_model=List[AdminTeacherAttendanceEntry])
def admin_teacher_attendance_cascade(
    date: date_type,
    batch_id: uuid.UUID,
    level_id: Optional[uuid.UUID] = None,
    subject_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Admin Teacher Attendance — View & Edit (full parity with the
    Coordinator's Day-Wise view, see GET /coordinator/day-wise, whose
    cascade rules this mirrors exactly). Cascading Selection: `batch_id` +
    `date` are always required — nothing renders before a Batch and a day
    are picked; `level_id`, `subject_id` progressively narrow that same
    day's periods in the enforced order Batch -> Level -> Subject. (Board
    was previously an extra step between Batch and Level; it's been
    removed along with the Board entity.)

    One row per period on `date` matching the filters, with the assigned
    teacher's OWN attendance status for that period+date (None if never
    marked at all — the Admin's screen renders that as an empty row ready
    to be marked). The write side is POST /admin/teacher-attendance below.
    """
    day_name = date.strftime("%A").lower()
    teacher = aliased(User)
    query = (
        db.query(
            TimetableSlot, Subject.name.label("subject_name"), Level.code.label("level_code"),
            teacher.full_name.label("teacher_name"), Batch.name.label("batch_name"),
        )
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(teacher, teacher.id == TimetableSlot.teacher_id)
        .join(Batch, Batch.id == TimetableSlot.batch_id)
        .outerjoin(Level, Level.id == TimetableSlot.level_id)
        .filter(
            TimetableSlot.batch_id == batch_id,
            TimetableSlot.day_of_week == day_name,
            TimetableSlot.deleted_at.is_(None),
        )
    )
    if level_id:
        query = query.filter(TimetableSlot.level_id == level_id)
    if subject_id:
        query = query.filter(TimetableSlot.subject_id == subject_id)
    rows = query.order_by(TimetableSlot.start_time).all()
    if not rows:
        return []

    active_pairs = active_offering_pairs(db, ((slot.batch_id, slot.subject_id) for slot, *_ in rows))

    slot_ids = [slot.id for slot, *_ in rows]
    teacher_id_by_slot = {slot.id: slot.teacher_id for slot, *_ in rows}
    # Only the TEACHER's own row per slot+date — never a student's — kept
    # by matching AttendanceRecord.user_id against that slot's assigned
    # teacher_id, same isolation the /coordinator/day-wise student counts
    # use in reverse (there it excludes the teacher; here it's the only
    # row wanted).
    candidate_records = db.query(AttendanceRecord).filter(
        AttendanceRecord.timetable_slot_id.in_(slot_ids),
        AttendanceRecord.date == date,
        AttendanceRecord.deleted_at.is_(None),
    ).all()
    records_by_slot = {
        r.timetable_slot_id: r
        for r in candidate_records
        if r.user_id == teacher_id_by_slot.get(r.timetable_slot_id)
    }

    result: List[AdminTeacherAttendanceEntry] = []
    for slot, subject_name, level_code, teacher_name, batch_name in rows:
        if (slot.batch_id, slot.subject_id) not in active_pairs:
            continue  # no active offering left for this slot's batch+subject
        record = records_by_slot.get(slot.id)
        result.append(AdminTeacherAttendanceEntry(
            timetable_slot_id=slot.id,
            date=date,
            start_time=slot.start_time,
            end_time=slot.end_time,
            batch_id=slot.batch_id,
            batch_name=batch_name,
            level_id=slot.level_id,
            level_code=level_code,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_id=slot.teacher_id,
            teacher_name=teacher_name,
            attendance_record_id=record.id if record else None,
            status=record.status if record else None,
            source=record.source if record else None,
            marked_by=record.marked_by if record else None,
            marked_at=record.marked_at if record else None,
        ))
    return result


@router.post("/admin/teacher-attendance", response_model=AttendanceRecordOut)
def mark_or_override_admin_teacher_attendance(
    payload: AdminTeacherAttendanceMarkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Admin Teacher Attendance — Mark & Edit (the write half of the cascade
    above). Marks, edits, or overrides ONE teacher's attendance for ONE
    timetable_slot_id + date. Works for any date, past or present —
    unlike the Teacher's own /mark-students, there's no "today only" lock
    here, matching the existing /coordinator/override-students and
    /teacher-override endpoints' "Admin/Coordinator can always correct
    history" behavior.

    Audit/Reason Logging: when this call changes an EXISTING attendance
    record (i.e. it's an edit/override of a status the teacher, a
    Coordinator, or a previous Admin action already set), `reason` is
    MANDATORY and is written to the audit_logs trail via log_action
    (entity_type='attendance_records', viewable at
    GET /api/audit-logs?entity_type=attendance_records&entity_id=...),
    exactly the same pattern PATCH /academics/marks/{id}/mark-override
    uses for override_reason. First-time marking (no existing record yet
    for this slot+date) does not require a reason, since there is nothing
    being corrected — mirrors POST /teacher-override, which has never
    required one for a first-time mark either.
    """
    slot = db.query(TimetableSlot).filter(
        TimetableSlot.id == payload.timetable_slot_id, TimetableSlot.deleted_at.is_(None)
    ).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timetable slot not found")
    if slot.teacher_id != payload.teacher_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="teacher_user_id does not match the teacher assigned to this timetable slot.",
        )
    if payload.subject_id != slot.subject_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject_id does not match the subject assigned to this timetable slot.",
        )

    # A lightweight pre-check: still needed here (unlike the write itself)
    # because the mandatory-reason-on-edit business rule has to be decided
    # — and enforced with a 400, doing nothing — before any DB mutation
    # happens at all. The actual write further down is still a single
    # atomic upsert; this SELECT only decides "is a reason required" and
    # what old_value to log, the same limited race noted in
    # POST /teacher-override above (mislabeling only, never a duplicate row).
    record = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == payload.teacher_user_id,
        AttendanceRecord.timetable_slot_id == payload.timetable_slot_id,
        AttendanceRecord.date == payload.date,
    ).first()

    if record:
        # Editing/overriding an existing status — reason is mandatory.
        if not payload.reason or not payload.reason.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A reason is required when editing or overriding an existing attendance record.",
            )
        old_value = {"status": record.status, "source": record.source}
        action = "teacher_attendance_overridden"
    else:
        old_value = None
        action = "teacher_attendance_marked"

    record = upsert_attendance_record(
        db,
        user_id=payload.teacher_user_id,
        subject_id=payload.subject_id,
        timetable_slot_id=payload.timetable_slot_id,
        date=payload.date,
        status=payload.status,
        marked_by=current_user.id,
        source="manual",
    )

    log_action(
        db, current_user.id, action, "attendance_records", record.id, old_value,
        {"status": payload.status, "reason": payload.reason},
    )

    if action == "teacher_attendance_overridden":
        teacher = db.query(User).filter(User.id == payload.teacher_user_id).first()
        if teacher:
            reason_suffix = f": {payload.reason}" if payload.reason else ""
            actor_label = current_user.role.capitalize() if current_user.role else "Admin"
            notify(
                db, teacher.id, "teacher_attendance_overridden",
                f"Your attendance for {payload.date.isoformat()} was changed to "
                f"'{payload.status}' by an {actor_label}{reason_suffix}.",
                related_entity_type="attendance_records", related_entity_id=record.id,
            )

    db.commit()
    return record


@router.get("/teachers/roster", response_model=List[TeacherRosterEntry])
def list_active_teachers(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """Active teachers for the Teacher Attendance Registry's row list."""
    teachers = db.query(User).filter(
        User.role == "teacher", User.status == "active", User.deleted_at.is_(None),
    ).order_by(User.full_name).all()
    return [TeacherRosterEntry(id=t.id, full_name=t.full_name) for t in teachers]


@router.get("/teachers/daily-log", response_model=List[TeacherDailyStatusEntry])
def get_teacher_daily_log(
    date: date_type,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    One row per active teacher for the given date: how many periods they
    have that day (from timetable_slots), and their current status if set.
    status is None when there are no periods that day, or when their
    periods disagree (e.g. one was individually overridden via
    /teacher-override) — the UI should show that as unset/needs-review
    rather than silently picking one.
    """
    day_name = date.strftime("%A").lower()
    teachers = db.query(User).filter(
        User.role == "teacher", User.status == "active", User.deleted_at.is_(None),
    ).order_by(User.full_name).all()

    result = []
    for teacher in teachers:
        slot_ids = [row.id for row in db.query(TimetableSlot.id).filter(
            TimetableSlot.teacher_id == teacher.id, TimetableSlot.day_of_week == day_name,
            TimetableSlot.deleted_at.is_(None),
        ).all()]

        status_value: Optional[str] = None
        if slot_ids:
            statuses = {
                row.status for row in db.query(AttendanceRecord.status).filter(
                    AttendanceRecord.user_id == teacher.id,
                    AttendanceRecord.timetable_slot_id.in_(slot_ids),
                    AttendanceRecord.date == date,
                    AttendanceRecord.deleted_at.is_(None),
                ).all()
            }
            if len(statuses) == 1:
                status_value = statuses.pop()

        result.append(TeacherDailyStatusEntry(
            teacher_user_id=teacher.id, full_name=teacher.full_name,
            period_count=len(slot_ids), status=status_value,
        ))
    return result


@router.post("/teachers/daily-log", response_model=TeacherDailyLogResult)
def save_teacher_daily_log(
    payload: TeacherDailyLogRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Bulk save from the Teacher Attendance Registry. Applies one status to
    every period the teacher has on this date. A teacher with zero periods
    that day can't get an attendance_records row — timetable_slot_id and
    subject_id are both NOT NULL on that table — so those are reported back
    in `skipped` rather than silently dropped or erroring the whole batch.

    entry.status is a Pydantic Literal (AttendanceStatusInput) on
    TeacherDailyLogEntry, so an invalid status is already rejected with a
    422 at request validation — no manual check needed per-entry here.
    """
    day_name = payload.date.strftime("%A").lower()

    updated_ids: List[uuid.UUID] = []
    skipped: List[TeacherDailyLogSkipped] = []

    for entry in payload.entries:
        slots = db.query(TimetableSlot).filter(
            TimetableSlot.teacher_id == entry.teacher_user_id, TimetableSlot.day_of_week == day_name,
            TimetableSlot.deleted_at.is_(None),
        ).all()

        if not slots:
            skipped.append(TeacherDailyLogSkipped(
                teacher_user_id=entry.teacher_user_id, reason="No periods scheduled on this date",
            ))
            continue

        for slot in slots:
            upsert_attendance_record(
                db,
                user_id=entry.teacher_user_id,
                subject_id=slot.subject_id,
                timetable_slot_id=slot.id,
                date=payload.date,
                status=entry.status,
                marked_by=current_user.id,
                source="manual",
            )
        updated_ids.append(entry.teacher_user_id)

    db.commit()
    return TeacherDailyLogResult(updated_teacher_ids=updated_ids, skipped=skipped)


@router.get("/me/summary", response_model=List[AttendanceSummaryOut])
def my_attendance_summary(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Per-subject aggregate for the Attendance Report card grid. Works for
    both students and teachers since it just aggregates AttendanceRecord
    rows where user_id == current_user.id, regardless of role.
    """
    rows = (
        db.query(
            AttendanceRecord.subject_id,
            Subject.name.label("subject_name"),
            Level.code.label("level_code"),
            func.sum(case((AttendanceRecord.status == "present", 1), else_=0)).label("present_count"),
            func.sum(case((AttendanceRecord.status == "absent", 1), else_=0)).label("absent_count"),
            func.sum(case((AttendanceRecord.status == "late", 1), else_=0)).label("late_count"),
            func.sum(case((AttendanceRecord.status == "excused", 1), else_=0)).label("excused_count"),
            func.count(AttendanceRecord.id).label("total_periods"),
        )
        .join(Subject, Subject.id == AttendanceRecord.subject_id)
        .outerjoin(Level, Level.id == Subject.level_id)
        .filter(AttendanceRecord.user_id == current_user.id, AttendanceRecord.deleted_at.is_(None))
        .group_by(AttendanceRecord.subject_id, Subject.name, Level.code)
        .order_by(Subject.name)
        .all()
    )

    result = []
    for r in rows:
        # "late" counts as attended for the percentage — a judgment call,
        # not something the schema dictates.
        attended = r.present_count + r.late_count
        pct = round((attended / r.total_periods) * 100, 1) if r.total_periods else 0.0
        result.append(AttendanceSummaryOut(
            subject_id=r.subject_id,
            subject_name=r.subject_name,
            level_code=r.level_code,
            present_count=r.present_count,
            absent_count=r.absent_count,
            late_count=r.late_count,
            excused_count=r.excused_count,
            total_periods=r.total_periods,
            attendance_percentage=pct,
        ))
    return result


@router.get("", response_model=List[AttendanceRecordDetailOut])
def list_attendance(
    user_id: Optional[uuid.UUID] = None,
    subject_id: Optional[uuid.UUID] = None,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        db.query(AttendanceRecord, Subject.name.label("subject_name"))
        .join(Subject, Subject.id == AttendanceRecord.subject_id)
        .filter(AttendanceRecord.deleted_at.is_(None))
    )
    if current_user.role in ("student", "teacher"):
        query = query.filter(AttendanceRecord.user_id == current_user.id)
    elif user_id:
        query = query.filter(AttendanceRecord.user_id == user_id)
    if subject_id:
        query = query.filter(AttendanceRecord.subject_id == subject_id)
    if date_from:
        query = query.filter(AttendanceRecord.date >= date_from)
    if date_to:
        query = query.filter(AttendanceRecord.date <= date_to)

    rows = query.order_by(AttendanceRecord.date.desc()).all()
    return [
        AttendanceRecordDetailOut(
            id=record.id,
            subject_id=record.subject_id,
            subject_name=subject_name,
            date=record.date,
            status=record.status,
            timetable_slot_id=record.timetable_slot_id,
        )
        for record, subject_name in rows
    ]


@router.delete("/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def void_attendance_record(
    record_id: uuid.UUID,
    reason: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Soft-delete / void a single attendance_records row — the "official
    retraction" path for a record that should never have counted at all
    (duplicate from a bug, wrong student entirely, wrong period, etc.).
    This is distinct from every /override, /teacher-override, and
    /admin/teacher-attendance endpoint above: those CORRECT a record's
    status in place and it still counts; this removes it from every read
    path in this router (every GET here already filters
    AttendanceRecord.deleted_at.is_(None)) and therefore from
    AttendanceSummaryOut totals, coordinator/admin day-wise counts, and
    the roster/history views, without destroying the row — the original
    status/source/marked_by/marked_at survive on the row itself, and the
    full old_value is written to audit_logs, for anyone who needs to see
    what a retracted record used to say.

    Scoped to Admin/Coordinator, same as every other correction endpoint
    in this file. A Teacher who marked something wrong today should
    just resubmit via POST /mark-students (an upsert — it overwrites the
    status in place); void is for retracting a record outside that
    same-day self-service window, or one a Teacher never marked at all.

    `reason` is mandatory — same accountability rule as an edit in
    POST /admin/teacher-attendance (retracting a record is at least as
    consequential as changing its status). It's a query param rather
    than a request body: this codebase's convention is that DELETE
    endpoints carry no body (see /content/materials/{id},
    /marks/{mark_id}, /subjects/{id}, /timetable/slots/{id}, etc.), and
    reason is the one extra piece of accountability data a void needs
    that those simpler deletes don't.

    Re-marking after a void: the (user_id, timetable_slot_id, date)
    unique constraint is keyed on identity, not on deleted_at, so a
    voided row is still the ON CONFLICT target if that same person is
    re-marked for that same period+date later. upsert_attendance_record()
    clears deleted_at on that path (see its docstring) specifically so a
    re-mark after a void correctly revives the row instead of leaving it
    permanently hidden behind what would otherwise look like a live,
    current status.
    """
    if not reason or not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A reason is required to void an attendance record.",
        )

    record = db.query(AttendanceRecord).filter(
        AttendanceRecord.id == record_id, AttendanceRecord.deleted_at.is_(None),
    ).first()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    old_value = {
        "user_id": str(record.user_id),
        "subject_id": str(record.subject_id),
        "timetable_slot_id": str(record.timetable_slot_id),
        "date": record.date.isoformat(),
        "status": record.status,
        "source": record.source,
        "marked_by": str(record.marked_by),
    }
    record.deleted_at = datetime.now(timezone.utc)

    log_action(
        db, current_user.id, "attendance_record_voided", "attendance_records", record.id,
        old_value, {"reason": reason},
    )

    affected_user = db.query(User).filter(User.id == record.user_id).first()
    if affected_user:
        actor_label = current_user.role.capitalize() if current_user.role else "Coordinator"
        notify(
            db, affected_user.id, "attendance_record_voided",
            f"Your '{record.status}' attendance record for {record.date.isoformat()} "
            f"was voided by a {actor_label}: {reason}",
            related_entity_type="attendance_records", related_entity_id=record.id,
        )

    db.commit()
    return None