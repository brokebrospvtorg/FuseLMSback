from sqlalchemy import Column, ForeignKey, TIMESTAMP, Date, Time, text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.enums import DayOfWeek, AttendanceStatus, AttendanceSource


class TimetableSlot(Base):
    __tablename__ = "timetable_slots"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    level_id = Column(UUID(as_uuid=True), ForeignKey("levels.id"), nullable=False)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    teacher_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    day_of_week = Column(DayOfWeek, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    subject_id = Column(UUID(as_uuid=True), ForeignKey("subjects.id"), nullable=False)
    timetable_slot_id = Column(UUID(as_uuid=True), ForeignKey("timetable_slots.id"), nullable=False)
    date = Column(Date, nullable=False)
    status = Column(AttendanceStatus, nullable=False)
    marked_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    source = Column(AttendanceSource, nullable=False)
    marked_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))
