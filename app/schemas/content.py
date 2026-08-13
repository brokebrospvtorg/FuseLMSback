import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class HelpingMaterialCreate(BaseModel):
    subject_id: uuid.UUID
    material_type: str  # notes | worksheet | past_paper | other
    title: str
    description: Optional[str] = None
    gcr_resource_id: Optional[str] = None
    gcr_link: str


class HelpingMaterialOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    material_type: str
    title: str
    description: Optional[str]
    gcr_link: str
    uploaded_by: uuid.UUID
    uploaded_at: datetime

    class Config:
        from_attributes = True


class LectureCreate(BaseModel):
    subject_id: uuid.UUID
    title: str
    description: Optional[str] = None
    youtube_video_id: str


class LectureOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    title: str
    description: Optional[str]
    youtube_video_id: str
    youtube_visibility: str
    uploaded_by: uuid.UUID
    uploaded_at: datetime

    class Config:
        from_attributes = True
