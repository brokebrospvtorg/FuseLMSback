from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, Numeric, Boolean, text
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.core.database import Base
from app.models.enums import AssessmentStatus


class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    name = Column(Text, nullable=False)
    max_marks = Column(Numeric, nullable=False)
    status = Column(AssessmentStatus, nullable=False, server_default="draft")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class Mark(Base):
    __tablename__ = "marks"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("assessments.id"), nullable=False)
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    marks_obtained = Column(Numeric, nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))


class MarkEditRequest(Base):
    """Teacher requests a change to marks that are already locked (saved);
    Coordinator (or Admin) approves/rejects. Same shape/flow as
    CorrectionRequest in models/user.py — status is plain TEXT + a CHECK
    constraint (schema_update.sql), not a PG enum, so no PGEnum wrapper here
    (matches how the column was actually created)."""
    __tablename__ = "mark_edit_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    mark_id = Column(UUID(as_uuid=True), ForeignKey("marks.id"), nullable=False)
    requested_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    requested_change = Column(JSONB, nullable=False)
    reason = Column(Text)
    status = Column(Text, nullable=False, server_default="pending")
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at = Column(TIMESTAMP(timezone=True))
    review_note = Column(Text)
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class GradingScheme(Base):
    __tablename__ = "grading_schemes"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    level_id = Column(UUID(as_uuid=True), ForeignKey("levels.id"), nullable=False)
    min_percentage = Column(Numeric, nullable=False)
    max_percentage = Column(Numeric, nullable=False)
    letter_grade = Column(Text, nullable=False)
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class Grade(Base):
    __tablename__ = "grades"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    computed_percentage = Column(Numeric)
    letter_grade = Column(Text)
    is_overridden = Column(Boolean, nullable=False, server_default="false")
    overridden_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    override_reason = Column(Text)
    last_computed_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    action = Column(Text, nullable=False)
    entity_type = Column(Text, nullable=False)
    entity_id = Column(UUID(as_uuid=True), nullable=False)
    old_value = Column(JSONB)
    new_value = Column(JSONB)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
