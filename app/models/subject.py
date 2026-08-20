from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.enums import ClassLevel


class ClassSubject(Base):
    """
    Subject & Class Management (Admin/Coordinator-only feature, integrated
    into the frontend's Academics section alongside Batches).

    Deliberately a NEW, separate table from `subjects` (app/models/academic.py)
    rather than a reuse of it:
      - The existing `subjects` table is a free-form catalog keyed off
        `levels.id` (an open-ended, admin-managed list) and backs Subject
        Requests, Enrollments, Teacher Assignments and Timetable already in
        production.
      - This feature's spec is narrower and stricter on purpose: a subject
        scoped to exactly one Batch, with `class_level` locked to 4 fixed
        strings rather than an arbitrary Level row.
    Overloading `subjects`/`levels` to also carry this stricter meaning
    would have meant either loosening the existing catalog's constraints or
    making one column mean two different things depending on context.
    Keeping this as its own table means this feature can't accidentally
    break the existing subject-request/enrollment/timetable flows.

    See schema_update_10_class_subjects.sql for the migration and
    app/schemas/subject.py's ClassLevelEnum for the frontend-facing mirror
    of the `class_level` Postgres enum.
    """

    __tablename__ = "class_subjects"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(Text, nullable=False)
    code = Column(Text, nullable=False)
    class_level = Column(ClassLevel, nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    description = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
