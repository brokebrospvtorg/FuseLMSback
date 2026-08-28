import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator

from app.schemas.common import ComplaintStatus as ComplaintStatusInput
from app.utils.sanitize import sanitize_required_text, sanitize_text


class ComplaintCreate(BaseModel):
    # Optional as of Sub-Sprint 6: a Teacher submitting general feedback
    # leaves this unset. Student/Parent submissions still provide it (the
    # router still enforces that for those two roles).
    student_id: Optional[uuid.UUID] = None
    subject_of_complaint: Optional[str] = None
    description: str

    # Stored-XSS defense-in-depth: strip any HTML/script payload out of
    # free-text input before it ever reaches the DB.
    @field_validator("description")
    @classmethod
    def _sanitize_description(cls, v: str) -> str:
        return sanitize_required_text(v)


class ComplaintUpdate(BaseModel):
    status: ComplaintStatusInput
    resolution_message: Optional[str] = None

    @field_validator("resolution_message")
    @classmethod
    def _sanitize_resolution_message(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_text(v)


class ComplaintOut(BaseModel):
    id: uuid.UUID
    submitted_by: uuid.UUID
    submitted_by_name: str
    submitted_by_role: str
    student_id: Optional[uuid.UUID] = None
    student_name: Optional[str] = None
    subject_of_complaint: Optional[str]
    description: str
    status: str
    resolved_by: Optional[uuid.UUID]
    resolved_at: Optional[datetime]
    resolution_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    type: str
    message: str
    related_entity_type: Optional[str]
    related_entity_id: Optional[uuid.UUID]
    channel: str
    read_at: Optional[datetime]
    sent_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True
