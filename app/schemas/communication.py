import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ComplaintCreate(BaseModel):
    student_id: uuid.UUID
    subject_of_complaint: Optional[str] = None
    description: str


class ComplaintUpdate(BaseModel):
    status: str  # open | in_progress | resolved


class ComplaintOut(BaseModel):
    id: uuid.UUID
    submitted_by: uuid.UUID
    student_id: uuid.UUID
    subject_of_complaint: Optional[str]
    description: str
    status: str
    resolved_by: Optional[uuid.UUID]
    resolved_at: Optional[datetime]
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
