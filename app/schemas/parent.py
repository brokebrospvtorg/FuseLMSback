import uuid
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel

from app.schemas.academic import SubjectOut


class ParentChildOut(BaseModel):
    """One row per linked child — what the Child Switcher widget lists."""
    student_id: uuid.UUID
    full_name: str
    roll_number: Optional[str] = None
    relationship: Optional[str] = None  # from parent_student_links.relationship — "Father", "Mother", etc.

    class Config:
        from_attributes = True


class ParentChildOverviewOut(BaseModel):
    """The three metric cards on the Performance Overview grid for one child."""
    student_id: uuid.UUID
    full_name: str
    current_batch_name: Optional[str] = None
    current_batch_year: Optional[int] = None
    overall_attendance_percentage: Optional[float] = None  # None = no attendance records yet
    aggregate_grade_percentage: Optional[float] = None  # None = no computed grades yet


class ParentMarkEntryOut(BaseModel):
    """One assessment's marks — same shape as student_grades.MarkEntryOut,
    duplicated here rather than imported so this schema module has no
    cross-dependency on another router's schema file."""
    assessment_id: uuid.UUID
    assessment_name: str
    max_marks: float
    marks_obtained: float


class ParentSubjectTranscriptOut(BaseModel):
    """One row of the Academic Transcript grid: a subject's overall grade
    plus the assessment-by-assessment breakdown underneath it.
    NOTE: the spec's transcript grid also asks for a 'Teacher Remarks'
    column — there is no remarks/comments field anywhere in the marks
    schema (see models/marks.py), so it can't be populated. Flagging
    rather than inventing a fake column; add a `remarks` column to
    `marks` first if this is actually needed."""
    subject_id: uuid.UUID
    subject_name: str
    computed_percentage: Optional[float] = None
    letter_grade: Optional[str] = None
    is_overridden: bool = False
    assessments: List[ParentMarkEntryOut] = []


class ParentSubjectAttendanceOut(BaseModel):
    """One subject's breakdown row on the Attendance View."""
    subject_id: uuid.UUID
    subject_name: str
    present_count: int
    absent_count: int
    late_count: int
    excused_count: int
    total_periods: int
    attendance_percentage: float


class ParentAttendanceActivityOut(BaseModel):
    """One row of the recent activity log."""
    date: date
    subject_name: str
    status: str


class ParentAttendanceSummaryOut(BaseModel):
    """GET /api/parent/child/{id}/attendance-summary — overall gauge numbers,
    the subject-wise breakdown table, and a recent activity feed, all in one
    call so the Attendance View doesn't need three separate requests."""
    student_id: uuid.UUID
    overall_present_count: int
    overall_absent_count: int
    overall_late_count: int
    overall_excused_count: int
    overall_total_periods: int
    overall_attendance_percentage: Optional[float] = None  # None = no records yet
    by_subject: List[ParentSubjectAttendanceOut]
    recent_activity: List[ParentAttendanceActivityOut]


class ParentTimetableEntryOut(BaseModel):
    """GET /api/parent/child/{id}/timetable — one scheduled period.
    NOTE: no room_number field — timetable_slots has no room column in the
    schema (see models/attendance.py). Flagging rather than inventing one;
    add the column first if room display is actually needed."""
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    teacher_name: str
    day_of_week: str
    period_number: int
    start_time: str
    end_time: str


class ParentAvailableSubjectsOut(BaseModel):
    """
    Wraps the subject list with the batch it was computed against — so the
    Subject Request form submits batch_id from THIS response, never from a
    separately-fetched "current batch" that could theoretically drift out
    of sync with what the backend actually filtered against.
    """
    batch_id: Optional[uuid.UUID] = None
    batch_name: Optional[str] = None
    subjects: List[SubjectOut] = []


class ParentSubjectRequestCreate(BaseModel):
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    # NOT persisted as its own column — subject_requests has no reason/comment
    # field (see 001_init_schema.sql), same situation as SubjectRequestReview.
    # comment in schemas/academic.py. Folded into the notification sent to
    # the Admin/Coordinator reviewers instead. Flag if you want an actual
    # `reason TEXT` column added via migration.
    reason: Optional[str] = None


class ParentSubjectRequestOut(BaseModel):
    """Joined with subject/batch names so the history table doesn't need
    N+1 lookups client-side — same pattern as SubjectRequestReviewRowOut
    on the Coordinator's review queue."""
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    batch_id: uuid.UUID
    batch_name: str
    status: str  # requested | approved | rejected
    requested_at: datetime
    actioned_at: Optional[datetime] = None
