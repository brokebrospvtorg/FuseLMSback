from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.enums import MaterialType, YoutubeVisibility


class HelpingMaterial(Base):
    __tablename__ = "helping_materials"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    material_type = Column(MaterialType, nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text)
    gcr_resource_id = Column(Text)
    gcr_link = Column(Text, nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))


class Lecture(Base):
    __tablename__ = "lectures"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text)
    youtube_video_id = Column(Text, nullable=False)
    youtube_visibility = Column(YoutubeVisibility, nullable=False, server_default="unlisted")
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))
