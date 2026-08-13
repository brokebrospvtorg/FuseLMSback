import uuid
from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel


class BatchCreate(BaseModel):
    session: str  # may_june | oct_nov
    year: int
    name: str
    start_date: date
    end_date: date
    is_current: bool = False


class BatchOut(BaseModel):
    id: uuid.UUID
    session: str
    year: int
    name: str
    start_date: date
    end_date: date
    is_current: bool
    created_at: datetime

    class Config:
        from_attributes = True


class LevelCreate(BaseModel):
    name: str
    display_order: int


class LevelOut(BaseModel):
    id: uuid.UUID
    name: str
    display_order: int

    class Config:
        from_attributes = True


class SubjectCreate(BaseModel):
    name: str
    code: Optional[str] = None
    level_id: uuid.UUID


class SubjectOut(BaseModel):
    id: uuid.UUID
    name: str
    code: Optional[str]
    level_id: uuid.UUID

    class Config:
        from_attributes = True


class StudentLevelEnrollmentCreate(BaseModel):
    student_id: uuid.UUID
    level_id: uuid.UUID
    started_at: date


class StudentLevelEnrollmentOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    level_id: uuid.UUID
    status: str
    started_at: date
    completed_at: Optional[date]

    class Config:
        from_attributes = True


class SubjectRequestCreate(BaseModel):
    subject_id: uuid.UUID
    batch_id: uuid.UUID


class SubjectRequestReview(BaseModel):
    status: str  # approved | rejected
    # Optional — not persisted as its own column (no migration for this sub-sprint),
    # but folded into the audit log's new_value and the student's notification message.
    comment: Optional[str] = None


class SubjectRequestOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    status: str
    requested_at: datetime
    actioned_by: Optional[uuid.UUID]
    actioned_at: Optional[datetime]

    class Config:
        from_attributes = True


class SubjectRequestReviewRowOut(BaseModel):
    """Coordinator/Admin review-queue shape — same row as SubjectRequestOut,
    joined with display names so the grid doesn't need N+1 lookups client-side."""
    id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    subject_id: uuid.UUID
    subject_name: str
    batch_id: uuid.UUID
    batch_name: str
    status: str
    requested_at: datetime
    actioned_by: Optional[uuid.UUID]
    actioned_at: Optional[datetime]


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    status: str
    enrolled_at: datetime

    class Config:
        from_attributes = True


class TeacherSubjectAssignmentCreate(BaseModel):
    teacher_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID


class TeacherSubjectAssignmentOut(BaseModel):
    id: uuid.UUID
    teacher_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    assigned_by: uuid.UUID
    assigned_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Student "me" views — enriched with joined display fields (subject_name,
# teacher_name) so the frontend doesn't need a second round-trip per row.
# ---------------------------------------------------------------------------
class TimetableEntryOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    teacher_name: str
    day_of_week: str
    period_number: int
    start_time: time
    end_time: time

    class Config:
        from_attributes = True


class DashboardSummaryOut(BaseModel):
    attendance_percentage: float
    pending_assessments_count: int
    current_batch_name: Optional[str]
    current_batch_year: Optional[int]
    active_subjects_count: int
