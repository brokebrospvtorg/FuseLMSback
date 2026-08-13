import uuid
from typing import List, Optional

from pydantic import BaseModel


class MarkEntryOut(BaseModel):
    assessment_id: uuid.UUID
    assessment_name: str
    max_marks: float
    marks_obtained: float


class SubjectMarksReportOut(BaseModel):
    subject_id: uuid.UUID
    subject_name: str
    assessments: List[MarkEntryOut]


class GradeReportEntryOut(BaseModel):
    subject_id: uuid.UUID
    subject_name: str
    computed_percentage: Optional[float]
    letter_grade: Optional[str]
    is_overridden: bool
