from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.enums import ComplaintStatus, NotificationChannel


class Complaint(Base):
    __tablename__ = "complaints"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    submitted_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    subject_of_complaint = Column(Text)
    description = Column(Text, nullable=False)
    status = Column(ComplaintStatus, nullable=False, server_default="open")
    resolved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    resolved_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    type = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    related_entity_type = Column(Text)
    related_entity_id = Column(UUID(as_uuid=True))
    channel = Column(NotificationChannel, nullable=False, server_default="in_app")
    read_at = Column(TIMESTAMP(timezone=True))
    sent_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
