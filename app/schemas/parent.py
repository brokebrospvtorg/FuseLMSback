import uuid
from typing import List, Optional

from pydantic import BaseModel


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
