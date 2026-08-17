import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ComplaintCreate(BaseModel):
    # Optional as of Sub-Sprint 6: a Teacher submitting general feedback
    # leaves this unset. Student/Parent submissions still provide it (the
    # router still enforces that for those two roles).
    student_id: Optional[uuid.UUID] = None
    subject_of_complaint: Optional[str] = None
    description: str


class ComplaintUpdate(BaseModel):
    status: str  # open | in_progress | resolved | closed
    resolution_message: Optional[str] = None


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
