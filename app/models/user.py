import uuid

from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, Date, Boolean, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.models.enums import UserRole, UserStatus, TokenType, CorrectionStatus


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    full_name = Column(Text, nullable=False)
    email = Column(Text, nullable=False)
    password_hash = Column(Text)
    role = Column(UserRole, nullable=False)
    status = Column(UserStatus, nullable=False, server_default="pending")
    phone_number = Column(Text)
    must_change_password = Column(Boolean, nullable=False, server_default="false")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    last_login_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))

    student_profile = relationship("StudentProfile", back_populates="user", uselist=False)
    teacher_profile = relationship("TeacherProfile", back_populates="user", uselist=False)
    parent_profile = relationship("ParentProfile", back_populates="user", uselist=False)


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    roll_number = Column(Text, unique=True)
    admission_date = Column(Date)
    father_name = Column(Text)
    date_of_birth = Column(Date)
    gender = Column(Text)
    religion = Column(Text)
    nationality = Column(Text)
    cnic = Column(Text)
    registration_id = Column(Text)

    user = relationship("User", back_populates="student_profile")


class TeacherProfile(Base):
    __tablename__ = "teacher_profiles"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    hire_date = Column(Date)
    gender = Column(Text)
    cnic = Column(Text)
    teacher_code = Column(Text)
    # designation Column REMOVED — see migration snippet below.

    user = relationship("User", back_populates="teacher_profile")
    levels = relationship(
        "TeacherLevel", back_populates="teacher_profile",
        cascade="all, delete-orphan", passive_deletes=True,
    )


class TeacherLevel(Base):
    """
    Teacher Multi-Level Assignment: which academic level(s) a Teacher is
    assigned to teach. Join table (teacher_id, level_id) — a normal
    queryable row per assignment, not a Postgres array column.
    """
    __tablename__ = "teacher_levels"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    teacher_id = Column(
        UUID(as_uuid=True), ForeignKey("teacher_profiles.user_id", ondelete="CASCADE"), nullable=False,
    )
    level_id = Column(UUID(as_uuid=True), ForeignKey("levels.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))

    teacher_profile = relationship("TeacherProfile", back_populates="levels")


class ParentProfile(Base):
    __tablename__ = "parent_profiles"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    cnic = Column(Text)
    registration_id = Column(Text)
    registration_date = Column(Date)

    user = relationship("User", back_populates="parent_profile")


class ParentStudentLink(Base):
    __tablename__ = "parent_student_links"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    parent_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    relationship_label = Column("relationship", Text)
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class VerificationToken(Base):
    __tablename__ = "verification_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    token = Column(Text, nullable=False, unique=True)
    token_type = Column(TokenType, nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=False)
    used_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class PasswordResetRequest(Base):
    """
    Admin-approval password reset flow (distinct from the existing
    email-token self-service flow backed by VerificationToken above).
    A logged-out user who doesn't have email access (or whose email isn't
    getting through) submits whatever identifier they have — email, roll
    number, or teacher/employee code — and an Admin reviews it manually
    from the Operations > Password Requests screen instead of a token
    round-trip. identifier_submitted is kept verbatim (not just user_id)
    so the Admin queue can show exactly what the requester typed, useful
    context if the lookup is ambiguous or the account can't be found.
    """
    __tablename__ = "password_reset_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    identifier_submitted = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="pending")  # pending | approved | rejected
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    review_note = Column(Text)
    reviewed_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class CorrectionRequest(Base):
    __tablename__ = "correction_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    requested_changes = Column(JSONB, nullable=False)
    status = Column(CorrectionStatus, nullable=False, server_default="pending")
    admin_notes = Column(Text)
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
