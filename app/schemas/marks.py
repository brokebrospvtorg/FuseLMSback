import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class AssessmentCreate(BaseModel):
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    name: str
    # gt=0: a zero/negative max_marks can't produce a meaningful percentage
    # (see calculate_percentage's own zero-guard in core/grading.py, which
    # exists as defense-in-depth for existing rows, not as license to allow
    # new ones in here).
    max_marks: Decimal = Field(..., gt=0)


class AssessmentUpdate(BaseModel):
    """Coordinator/Admin direct-edit — deliberately narrower than
    AssessmentCreate: subject_id/batch_id/status aren't editable here
    (moving an assessment between classes, or un-publishing one that
    students may have already seen, are separate decisions this endpoint
    doesn't make for you)."""
    name: Optional[str] = None
    max_marks: Optional[Decimal] = Field(default=None, gt=0)


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
    # Mark Override refactor (schema_update_18): audit trail for a
    # Coordinator/Admin correction via PATCH /marks/{mark_id}/mark-override.
    # False/None for a mark that's only ever gone through the normal
    # teacher upload/upsert path.
    is_overridden: bool = False
    overridden_by: Optional[uuid.UUID] = None

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
    min_percentage: Decimal = Field(..., ge=0, le=100)
    max_percentage: Decimal = Field(..., ge=0, le=100)
    letter_grade: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check_bounds(self) -> "GradingSchemeCreate":
        # A band where min > max can never match a computed percentage —
        # that band (and every student who should fall in it) would
        # silently never get this letter grade from this scheme.
        if self.min_percentage > self.max_percentage:
            raise ValueError("min_percentage cannot be greater than max_percentage")
        return self


class GradingSchemeOut(BaseModel):
    id: uuid.UUID
    level_id: uuid.UUID
    min_percentage: Decimal
    max_percentage: Decimal
    letter_grade: str

    class Config:
        from_attributes = True


class MarkOverrideRequest(BaseModel):
    """Mark Override refactor (schema_update_18): body for PATCH
    /api/academics/marks/{mark_id}/mark-override — replaces the removed
    GradeOverrideRequest/PATCH .../grades/{id}/override. Corrects one
    student's score on one assessment (not a subject-level letter grade);
    the pooled percentage/grade is derived automatically afterwards."""
    marks_obtained: Decimal
    override_reason: str = Field(..., min_length=1)


class RosterEntryOut(BaseModel):
    student_id: uuid.UUID
    full_name: str
    roll_number: Optional[str]


class GradeOut(BaseModel):
    """Mark Override refactor (schema_update_18): Grade is now a purely
    computed rollup — no is_overridden/overridden_by/override_reason here
    anymore, since a Grade can no longer be overridden directly. To see
    whether an individual assessment mark behind this percentage was
    corrected, check MarkOut.is_overridden for that assessment via
    GET /api/academics/assessments/{assessment_id}/marks."""
    id: uuid.UUID
    student_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    computed_percentage: Optional[Decimal]
    letter_grade: Optional[str]
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
