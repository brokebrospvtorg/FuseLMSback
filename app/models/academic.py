from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, Date, Integer, Boolean, text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.enums import (
    BatchSession, LevelEnrollmentStatus, SubjectRequestStatus, EnrollmentStatus, Board,
)


class Batch(Base):
    __tablename__ = "batches"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    session = Column(BatchSession, nullable=False)
    year = Column(Integer, nullable=False)
    name = Column(Text, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    is_current = Column(Boolean, nullable=False, server_default="false")
    # schema_update_11: the examining board a Batch is created under.
    # Required (NOT NULL) for every Batch, but schema_update_15 downgrades
    # what this column MEANS: a Batch is not restricted to this one board —
    # it's just the default/originating board captured at creation time
    # (and editable via PUT .../batches/{id} for when Admin picked wrong).
    # Whether a Batch has real activity under British Council, Edexcel,
    # and/or LRN is answered by BatchSubject.board below, not this column —
    # do not filter/gate batch visibility by this field (see
    # routers/academic.py list_batches' active_boards and
    # admin-batches.component.ts).
    board = Column(Board, nullable=False)
    # schema_update_13: "is this batch open for admin work" — assigning
    # teachers, offering subjects, taking subject requests — independent of
    # is_current above (see that migration's comment for the distinction).
    is_active = Column(Boolean, nullable=False, server_default="true")
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class Level(Base):
    """
    schema_update_16: `code` + `is_active` added so the catalog can be
    pinned to exactly 4 standardized rows (O-LEVEL / AS-LEVEL / A2-LEVEL /
    A-LEVEL) without hard-deleting legacy rows that older
    student_level_enrollments / subjects / enrollments rows still FK to.
    GET /api/academic/levels now filters on is_active + the 4 known codes
    (see routers/academic.py) rather than just deleted_at.
    """
    __tablename__ = "levels"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(Text, nullable=False)
    code = Column(Text, nullable=False)
    display_order = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class Subject(Base):
    """
    Cambridge subject catalog. schema_update_16 reverses schema_update_11's
    "no code, no create endpoint" decision: `code` and `is_active` are back,
    and POST /api/academic/subjects exists again (see routers/academic.py).

    `level_id` is INTENTIONALLY KEPT as the single "primary" level FK —
    offer_subjects_for_batch / list_offered_subjects / TeacherSubjectAssignment
    / Enrollment.level_id all key off it and are out of scope for this
    change. When a subject is created against multiple levels via the new
    Add Subject dialog, level_id is set to the first selected level_id and
    every selected level (including that one) is additionally written to
    `subject_levels` — see SubjectLevel below, which is the real multi-level
    mapping and the one the Add Subject dialog's checkboxes populate.
    """
    __tablename__ = "subjects"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(Text, nullable=False)
    code = Column(Text, nullable=False)
    level_id = Column(UUID(as_uuid=True), ForeignKey("levels.id"), nullable=False)
    board = Column(Board, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class SubjectLevel(Base):
    """
    schema_update_16: many-to-many Subject <-> Level mapping ("Offered
    Levels" checkboxes on the Add Subject dialog). Additive alongside
    Subject.level_id, not a replacement for it — see Subject's docstring.
    """
    __tablename__ = "subject_levels"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    level_id = Column(UUID(as_uuid=True), ForeignKey("levels.id"), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class StudentLevelEnrollment(Base):
    __tablename__ = "student_level_enrollments"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    level_id = Column(UUID(as_uuid=True), ForeignKey("levels.id"), nullable=False)
    status = Column(LevelEnrollmentStatus, nullable=False, server_default="active")
    started_at = Column(Date, nullable=False)
    completed_at = Column(Date)
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class SubjectRequest(Base):
    __tablename__ = "subject_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    status = Column(SubjectRequestStatus, nullable=False, server_default="requested")
    requested_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    actioned_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    actioned_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    # Cross-Level Subject Enrollment (schema_update_7.sql): the level
    # curriculum this specific enrollment was made under — set from the
    # subject's own level_id at write time, independent of the student's
    # primary level_id in student_level_enrollments. Nullable only because
    # older rows predate this column; every row created via update_user()
    # (app/routers/users.py) always sets it.
    level_id = Column(UUID(as_uuid=True), ForeignKey("levels.id"))
    status = Column(EnrollmentStatus, nullable=False, server_default="active")
    enrolled_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))


class TeacherSubjectAssignment(Base):
    __tablename__ = "teacher_subject_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    teacher_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    assigned_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    assigned_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))


class BatchSubject(Base):
    """
    schema_update_13: Admin's explicit "this subject is offered in this
    batch" declaration — what GET/POST /api/academic/batches/{id}/offered-subjects
    read and write. Deliberately separate from (and upstream of) actual
    Enrollment/TeacherSubjectAssignment activity: a subject has to be
    offered here FIRST before any Student/Parent can request or enroll in
    it, rather than the older approach (see routers/batches.py's summary
    endpoint) of inferring "active subjects" after the fact from who's
    already taken it — that approach works for a retrospective admin
    summary, but can't answer "what should show up in the request
    dropdown before anyone's enrolled," which is the actual bug this fixes.

    schema_update_15: `board` records which examining Board this specific
    offering is under. A Batch is NOT restricted to a single Board (see
    Batch.board's own docstring) — the same catalog Subject can be offered
    more than once in the same batch, once per Board it's actually running
    under here (unique on batch_id+subject_id+board, not just
    batch_id+subject_id). This is the column that makes "is this batch
    active under Edexcel" answerable at all.
    """
    __tablename__ = "batch_subjects"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    board = Column(Board, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
