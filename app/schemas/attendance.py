import uuid
from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel

from app.schemas.common import BoardEnum


class TimetableSlotCreate(BaseModel):
    level_id: uuid.UUID
    subject_id: uuid.UUID
    teacher_id: uuid.UUID
    batch_id: uuid.UUID
    day_of_week: str
    period_number: int
    start_time: time
    end_time: time


class TimetableSlotOut(BaseModel):
    id: uuid.UUID
    level_id: uuid.UUID
    subject_id: uuid.UUID
    teacher_id: uuid.UUID
    batch_id: uuid.UUID
    day_of_week: str
    period_number: int
    start_time: time
    end_time: time

    class Config:
        from_attributes = True


class StudentAttendanceMarkItem(BaseModel):
    student_user_id: uuid.UUID
    status: str  # present | absent | late | excused


class StudentAttendanceMarkRequest(BaseModel):
    """Teacher marks attendance for their own students for one period."""
    timetable_slot_id: uuid.UUID
    subject_id: uuid.UUID
    date: date
    records: list[StudentAttendanceMarkItem]


class TeacherAttendanceOverrideRequest(BaseModel):
    """Coordinator manually marks/overrides a teacher's attendance for a period they never logged into."""
    timetable_slot_id: uuid.UUID
    subject_id: uuid.UUID
    teacher_user_id: uuid.UUID
    date: date
    status: str


class TeacherDailyLogEntry(BaseModel):
    teacher_user_id: uuid.UUID
    status: str  # present | absent | late | excused (UI's "Leave" maps to excused — see router note)


class TeacherDailyLogRequest(BaseModel):
    """Bulk save from the Teacher Attendance Registry screen — one status per
    teacher, applied to every period that teacher has on this date."""
    date: date
    entries: list[TeacherDailyLogEntry]


class TeacherDailyLogSkipped(BaseModel):
    teacher_user_id: uuid.UUID
    reason: str


class TeacherDailyLogResult(BaseModel):
    """What actually happened — since a teacher with zero periods that day
    can't get an attendance_records row (timetable_slot_id is NOT NULL),
    those are reported back as skipped rather than silently dropped."""
    updated_teacher_ids: list[uuid.UUID]
    skipped: list[TeacherDailyLogSkipped]


class TeacherRosterEntry(BaseModel):
    id: uuid.UUID
    full_name: str


class TeacherDailyStatusEntry(BaseModel):
    teacher_user_id: uuid.UUID
    full_name: str
    period_count: int
    status: Optional[str]  # None if no periods that day, or if periods disagree (mixed manual overrides)


class PeriodRecordOut(BaseModel):
    """One student's existing record for a specific period+date — used by
    the Teacher's Mark Attendance screen to detect 'already submitted' and
    render a locked/read-only view instead of the editable grid."""
    student_user_id: uuid.UUID
    status: str
    marked_at: datetime


class AttendanceRecordOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    subject_id: uuid.UUID
    timetable_slot_id: uuid.UUID
    date: date
    status: str
    marked_by: uuid.UUID
    source: str
    marked_at: datetime

    class Config:
        from_attributes = True


class AttendanceRecordDetailOut(BaseModel):
    """Same as AttendanceRecordOut but with subject_name joined in —
    the frontend log table needs a readable name, not just a UUID."""
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    date: date
    status: str
    timetable_slot_id: uuid.UUID


class AttendanceSummaryOut(BaseModel):
    """Per-subject aggregate for the Attendance Report card grid.
    attendance_percentage counts 'late' as attended, consistent with how
    most schools calculate it — flagged here since it's a judgment call,
    not something the schema dictates."""
    subject_id: uuid.UUID
    subject_name: str
    # DB level code (e.g. "AS-LEVEL") for the frontend's level badge — see
    # app/core/grading.py::LEVEL_ABBREVIATIONS. None if unset/soft-deleted.
    level_code: Optional[str] = None
    present_count: int
    absent_count: int
    late_count: int
    excused_count: int
    total_periods: int
    attendance_percentage: float


class TimetableSlotDetailOut(BaseModel):
    """Same slot data as TimetableSlotOut but with subject_name, teacher_name,
    and batch_name joined in — the slot-grid UI needs readable labels, not
    raw UUIDs, to render each cell. batch_name added Sub-Sprint 6.1 for the
    Teacher's weekly grid ("Class Name" column in the sprint plan)."""
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    teacher_id: uuid.UUID
    teacher_name: str
    batch_id: uuid.UUID
    batch_name: str
    day_of_week: str
    period_number: int
    start_time: time
    end_time: time
    # Over-Inclusive Cascading Dropdowns fix: which active BatchSubject
    # board this slot's subject+batch is actually offered under, resolved
    # server-side — same idea as TeacherSubjectAssignmentOut.board (see
    # that field's docstring in schemas/academic.py). Populated (and the
    # slot fanned out once per active board) only on Teacher-scoped reads
    # (GET /timetable/slots for a Teacher, GET /timetable/my-teaching-schedule)
    # where a board-accurate cascade actually matters; left None on the
    # Admin/Coordinator Interactive Timetable Builder's unfiltered listing,
    # which intentionally still needs to see every slot regardless of
    # offering status in order to fix ones that drifted out of sync.
    board: Optional[BoardEnum] = None


class CoordinatorRosterEntry(BaseModel):
    """A student in a timetable slot's class, plus their current attendance
    status for the given date (None if nothing recorded yet at all) — what
    the Coordinator's edit screen renders as one editable row."""
    student_user_id: uuid.UUID
    full_name: str
    status: Optional[str] = None


class CoordinatorStudentOverrideRequest(BaseModel):
    """Bulk upsert, no ownership restriction — this is the 'bypass the
    teacher lock' path. Same shape as StudentAttendanceMarkRequest but
    lives on its own endpoint (admin/coordinator only) rather than
    overloading the Teacher's endpoint with a role branch."""
    timetable_slot_id: uuid.UUID
    subject_id: uuid.UUID
    date: date
    records: list[StudentAttendanceMarkItem]


class TeacherAttendanceLogEntry(BaseModel):
    """One row per class (period+date) the teacher has already taken —
    the Day-Wise UI's 'View Summary' history table. Aggregates the
    student-level AttendanceRecord rows for that period+date into counts
    so the table doesn't need to render one row per student."""
    date: date
    timetable_slot_id: uuid.UUID
    period_number: int
    subject_id: uuid.UUID
    subject_name: str
    level_code: Optional[str] = None
    present_count: int
    absent_count: int
    late_count: int
    excused_count: int
    total_students: int


class TimetableSlotUpdate(BaseModel):
    """Interactive Timetable Builder (Coordinator Portal Sub-Sprint 3) —
    every field optional so a single PATCH can move a slot to a different
    day/period, reassign the teacher, or retime it, without needing a
    delete+recreate (which would also orphan any attendance already taken
    against the old slot_id)."""
    level_id: Optional[uuid.UUID] = None
    subject_id: Optional[uuid.UUID] = None
    teacher_id: Optional[uuid.UUID] = None
    batch_id: Optional[uuid.UUID] = None
    day_of_week: Optional[str] = None
    period_number: Optional[int] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
