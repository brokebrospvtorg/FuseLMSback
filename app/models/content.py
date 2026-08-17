from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, Boolean, text
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
    # Lectures Sub-Sprint 1 (YouTube): nullable as of schema_update_6.sql —
    # a lecture can now be created before its video is set (POST
    # .../youtube-video sets it once, Sub-Sprint 2), same two-step shape as
    # classroom_url below. Old rows created under the previous
    # mandatory-at-creation flow keep their value and are backfilled locked.
    youtube_video_id = Column(Text, nullable=True)
    youtube_video_id_locked = Column(Boolean, nullable=False, server_default="false")
    youtube_visibility = Column(YoutubeVisibility, nullable=False, server_default="unlisted")
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))

    # Lectures Sub-Sprint 1: Google Classroom link, set once by the Teacher,
    # then locked. Further changes must go through classroom_edit_requests.
    classroom_url = Column(Text, nullable=True)
    classroom_url_locked = Column(Boolean, nullable=False, server_default="false")


class ClassroomEditRequest(Base):
    __tablename__ = "classroom_edit_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    lecture_id = Column(UUID(as_uuid=True), ForeignKey("lectures.id"), nullable=False)
    requested_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    proposed_url = Column(Text, nullable=False)
    reason = Column(Text)
    status = Column(Text, nullable=False, server_default="pending")  # pending | approved | rejected
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    review_note = Column(Text)
    reviewed_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class YoutubeEditRequest(Base):
    """Lectures Sub-Sprint 1 (YouTube): mirrors ClassroomEditRequest exactly
    (schema_update_6.sql) — Teacher's proposed replacement for an
    already-locked youtube_video_id, reviewed by Coordinator/Admin."""
    __tablename__ = "youtube_edit_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    lecture_id = Column(UUID(as_uuid=True), ForeignKey("lectures.id"), nullable=False)
    requested_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    proposed_url = Column(Text, nullable=False)  # raw URL; parsed to an 11-char id at review time
    reason = Column(Text)
    status = Column(Text, nullable=False, server_default="pending")  # pending | approved | rejected
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    review_note = Column(Text)
    reviewed_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
