import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class AssessmentCreate(BaseModel):
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    name: str
    max_marks: Decimal


class AssessmentOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    name: str
    max_marks: Decimal
    status: str
    created_by: uuid.UUID
    created_at: datetime

    class Config:
        from_attributes = True


class MarkUpsert(BaseModel):
    student_id: uuid.UUID
    marks_obtained: Decimal


class MarkOut(BaseModel):
    id: uuid.UUID
    assessment_id: uuid.UUID
    student_id: uuid.UUID
    marks_obtained: Decimal
    uploaded_by: uuid.UUID
    uploaded_at: datetime

    class Config:
        from_attributes = True


class GradingSchemeCreate(BaseModel):
    level_id: uuid.UUID
    min_percentage: Decimal
    max_percentage: Decimal
    letter_grade: str


class GradingSchemeOut(BaseModel):
    id: uuid.UUID
    level_id: uuid.UUID
    min_percentage: Decimal
    max_percentage: Decimal
    letter_grade: str

    class Config:
        from_attributes = True


class GradeOverrideRequest(BaseModel):
    letter_grade: str
    override_reason: str = Field(..., min_length=1)


class RosterEntryOut(BaseModel):
    student_id: uuid.UUID
    full_name: str
    roll_number: Optional[str]


class GradeOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    computed_percentage: Optional[Decimal]
    letter_grade: Optional[str]
    is_overridden: bool
    overridden_by: Optional[uuid.UUID]
    override_reason: Optional[str]
    last_computed_at: Optional[datetime]

    class Config:
        from_attributes = True


class AuditLogOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID
    old_value: Optional[dict]
    new_value: Optional[dict]
    created_at: datetime

    class Config:
        from_attributes = True
