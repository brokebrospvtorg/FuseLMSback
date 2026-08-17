import uuid
from datetime import date as date_type
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.models import AttendanceRecord, TimetableSlot, User, Subject, Enrollment
from app.schemas.attendance import (
    StudentAttendanceMarkRequest, TeacherAttendanceOverrideRequest, AttendanceRecordOut,
    AttendanceRecordDetailOut, AttendanceSummaryOut, PeriodRecordOut,
    TeacherDailyLogRequest, TeacherDailyLogResult, TeacherDailyLogSkipped,
    TeacherRosterEntry, TeacherDailyStatusEntry,
    CoordinatorRosterEntry, CoordinatorStudentOverrideRequest,
)

router = APIRouter(prefix="/api/attendance", tags=["attendance"], dependencies=[Depends(check_license)])


@router.post("/mark-students", response_model=List[AttendanceRecordOut], status_code=status.HTTP_201_CREATED)
def mark_student_attendance(
    payload: StudentAttendanceMarkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher")),
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

    created: List[AttendanceRecord] = []
    for item in payload.records:
        existing = db.query(AttendanceRecord).filter(
            AttendanceRecord.user_id == item.student_user_id,
            AttendanceRecord.timetable_slot_id == payload.timetable_slot_id,
            AttendanceRecord.date == payload.date,
        ).first()
        if existing:
            existing.status = item.status
            existing.marked_by = current_user.id
            created.append(existing)
        else:
            record = AttendanceRecord(
                user_id=item.student_user_id,
                subject_id=payload.subject_id,
                timetable_slot_id=payload.timetable_slot_id,
                date=payload.date,
                status=item.status,
                marked_by=current_user.id,
                source="manual",
            )
            db.add(record)
            created.append(record)

    # Auto-mark the teacher present for this period, if not already recorded.
    teacher_record = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == current_user.id,
        AttendanceRecord.timetable_slot_id == payload.timetable_slot_id,
        AttendanceRecord.date == payload.date,
    ).first()
    if not teacher_record:
        db.add(AttendanceRecord(
            user_id=current_user.id,
            subject_id=payload.subject_id,
            timetable_slot_id=payload.timetable_slot_id,
            date=payload.date,
            status="present",
            marked_by=current_user.id,
            source="auto",
        ))

    db.commit()
    for r in created:
        db.refresh(r)
    return created


@router.get("/my-period-records", response_model=List[PeriodRecordOut])
def get_my_period_records(
    timetable_slot_id: uuid.UUID,
    date: date_type,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("teacher")),
):
    """
    Sub-Sprint 3.2 — lets the Teacher's Mark Attendance screen check
    whether a period+date was already submitted (so it can render a
    locked, read-only view instead of the editable grid), and lets a
    past date be inspected read-only. Excludes the teacher's own
    auto-marked row — this is student records only.
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
    slots = query.all()

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
                "period_number": slot.period_number,
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
    Sub-Sprint 3 (Coordinator Portal): the Teacher's own roster fetch
    (`/my-period-records`) is locked to `slot.teacher_id == current_user.id`
    — which is exactly right for a Teacher, but means there was previously
    NO way for a Coordinator to pull up a class roster at all, for either a
    slot the teacher skipped entirely or one they already submitted. This
    is the roster-only half of "edit capability for previous dates' student
    attendance, bypassing the teacher lock" — every enrolled student for
    the slot's subject+batch, with whatever attendance status already
    exists for this date (None if nothing recorded yet).
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
    The write half of the same gap: Teacher's `/mark-students` is
    role-locked to Teacher AND, once saved, the Teacher's own frontend
    treats that period as read-only going forward (no lock flag on the DB
    row itself — the lock is enforced by which endpoint can write to it).
    This endpoint is that bypass, scoped to Admin/Coordinator only, for any
    date past or present. Deliberately does NOT touch the teacher's own
    auto-marked attendance row for this slot — that's edited separately via
    POST /api/attendance/teacher-override, so a Coordinator correcting a
    student's mistaken entry doesn't accidentally reset the teacher's mark.
    """
    slot = db.query(TimetableSlot).filter(
        TimetableSlot.id == payload.timetable_slot_id, TimetableSlot.deleted_at.is_(None)
    ).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timetable slot not found")

    saved: List[AttendanceRecord] = []
    for item in payload.records:
        existing = db.query(AttendanceRecord).filter(
            AttendanceRecord.user_id == item.student_user_id,
            AttendanceRecord.timetable_slot_id == payload.timetable_slot_id,
            AttendanceRecord.date == payload.date,
        ).first()
        if existing:
            existing.status = item.status
            existing.marked_by = current_user.id
            saved.append(existing)
        else:
            record = AttendanceRecord(
                user_id=item.student_user_id,
                subject_id=payload.subject_id,
                timetable_slot_id=payload.timetable_slot_id,
                date=payload.date,
                status=item.status,
                marked_by=current_user.id,
                source="manual",
            )
            db.add(record)
            saved.append(record)

    db.commit()
    for r in saved:
        db.refresh(r)
    return saved


@router.post("/teacher-override", response_model=AttendanceRecordOut, status_code=status.HTTP_201_CREATED)
def coordinator_mark_or_override_teacher(
    payload: TeacherAttendanceOverrideRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("coordinator", "admin")),
):
    """Coordinator manually marks (absent/late/excused) or overrides a teacher's attendance for a period."""
    record = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == payload.teacher_user_id,
        AttendanceRecord.timetable_slot_id == payload.timetable_slot_id,
        AttendanceRecord.date == payload.date,
    ).first()
    if record:
        record.status = payload.status
        record.marked_by = current_user.id
        record.source = "manual"
    else:
        record = AttendanceRecord(
            user_id=payload.teacher_user_id,
            subject_id=payload.subject_id,
            timetable_slot_id=payload.timetable_slot_id,
            date=payload.date,
            status=payload.status,
            marked_by=current_user.id,
            source="manual",
        )
        db.add(record)
    db.commit()
    db.refresh(record)
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
    status is None when there are no periods that day (nothing to mark), or
    when their periods disagree (e.g. one was individually overridden via
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
    """
    valid_statuses = {"present", "absent", "late", "excused"}
    day_name = payload.date.strftime("%A").lower()

    updated_ids: List[uuid.UUID] = []
    skipped: List[TeacherDailyLogSkipped] = []

    for entry in payload.entries:
        if entry.status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{entry.status}' for teacher {entry.teacher_user_id}",
            )

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
            record = db.query(AttendanceRecord).filter(
                AttendanceRecord.user_id == entry.teacher_user_id,
                AttendanceRecord.timetable_slot_id == slot.id,
                AttendanceRecord.date == payload.date,
            ).first()
            if record:
                record.status = entry.status
                record.marked_by = current_user.id
                record.source = "manual"
            else:
                db.add(AttendanceRecord(
                    user_id=entry.teacher_user_id, subject_id=slot.subject_id,
                    timetable_slot_id=slot.id, date=payload.date,
                    status=entry.status, marked_by=current_user.id, source="manual",
                ))
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
            func.sum(case((AttendanceRecord.status == "present", 1), else_=0)).label("present_count"),
            func.sum(case((AttendanceRecord.status == "absent", 1), else_=0)).label("absent_count"),
            func.sum(case((AttendanceRecord.status == "late", 1), else_=0)).label("late_count"),
            func.sum(case((AttendanceRecord.status == "excused", 1), else_=0)).label("excused_count"),
            func.count(AttendanceRecord.id).label("total_periods"),
        )
        .join(Subject, Subject.id == AttendanceRecord.subject_id)
        .filter(AttendanceRecord.user_id == current_user.id, AttendanceRecord.deleted_at.is_(None))
        .group_by(AttendanceRecord.subject_id, Subject.name)
        .order_by(Subject.name)
        .all()
    )

    result = []
    for r in rows:
        # "late" counts as attended for the percentage — a judgment call,
        # not something the schema dictates. Change here if the academy
        # wants late marked separately.
        attended = r.present_count + r.late_count
        pct = round((attended / r.total_periods) * 100, 1) if r.total_periods else 0.0
        result.append(AttendanceSummaryOut(
            subject_id=r.subject_id,
            subject_name=r.subject_name,
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
