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


class AssessmentUpdate(BaseModel):
    """Coordinator/Admin direct-edit — deliberately narrower than
    AssessmentCreate: subject_id/batch_id/status aren't editable here
    (moving an assessment between classes, or un-publishing one that
    students may have already seen, are separate decisions this endpoint
    doesn't make for you)."""
    name: Optional[str] = None
    max_marks: Optional[Decimal] = None


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


class MarkEditRequestCreate(BaseModel):
    requested_change: dict  # e.g. {"marks_obtained": 82}
    reason: Optional[str] = None


class MarkEditRequestReview(BaseModel):
    status: str  # approved | rejected
    # Coordinator/Admin's note when approving/rejecting — same optional
    # reviewer-note pattern as FeeProofReview.rejection_reason and
    # ComplaintUpdate.resolution_message.
    review_note: Optional[str] = None


class MarkEditRequestOut(BaseModel):
    id: uuid.UUID
    mark_id: uuid.UUID
    requested_by: uuid.UUID
    requested_change: dict
    reason: Optional[str] = None
    status: str
    reviewed_by: Optional[uuid.UUID] = None
    reviewed_at: Optional[datetime] = None
    review_note: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MarkEditRequestWithContextOut(MarkEditRequestOut):
    """Sub-Sprint 5.2's status-tracking list needs more than raw IDs to be
    readable — subject/assessment/student names, plus what the mark is
    currently set to, so the Teacher can see "before" next to "requested"
    without a second round-trip per row."""
    assessment_name: str
    subject_name: str
    student_name: str
    current_marks_obtained: Decimal


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
