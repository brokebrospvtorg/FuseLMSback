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
    # DB level code (e.g. "AS-LEVEL") so the frontend can render the short
    # badge ("Mathematics [AS]") — see app/core/grading.py for the
    # code -> abbreviation map. None if the subject's level was soft-deleted.
    level_code: Optional[str] = None
    assessments: List[MarkEntryOut]


class GradeReportEntryOut(BaseModel):
    subject_id: uuid.UUID
    subject_name: str
    level_code: Optional[str] = None
    computed_percentage: Optional[float]
    letter_grade: Optional[str]
    # NOTE: `is_overridden` / `overridden_by` / `override_reason` are
    # DELIBERATELY NOT exposed here. Students see only their clean final
    # percentage and derived grade — no "Overridden" badge or coordinator
    # metadata (see routers/marks.py::override_grade / GradeOut, which is
    # the admin/coordinator-facing schema and does carry that metadata).
