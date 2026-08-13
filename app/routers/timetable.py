import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, aliased

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.models import TimetableSlot, Enrollment, User, Subject
from app.schemas.attendance import TimetableSlotCreate, TimetableSlotOut, TimetableSlotDetailOut

router = APIRouter(prefix="/api/timetable", tags=["timetable"], dependencies=[Depends(check_license)])


@router.post("/slots", response_model=TimetableSlotOut, status_code=status.HTTP_201_CREATED)
def create_slot(payload: TimetableSlotCreate, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    slot = TimetableSlot(**payload.model_dump())
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


@router.get("/slots", response_model=List[TimetableSlotOut])
def list_slots(batch_id: Optional[uuid.UUID] = None, teacher_id: Optional[uuid.UUID] = None,
                db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = db.query(TimetableSlot).filter(TimetableSlot.deleted_at.is_(None))
    if batch_id:
        query = query.filter(TimetableSlot.batch_id == batch_id)
    if teacher_id:
        query = query.filter(TimetableSlot.teacher_id == teacher_id)
    if current_user.role == "teacher":
        query = query.filter(TimetableSlot.teacher_id == current_user.id)
    return query.order_by(TimetableSlot.day_of_week, TimetableSlot.period_number).all()


@router.get("/my-timetable", response_model=List[TimetableSlotDetailOut])
def my_timetable(db: Session = Depends(get_db), current_user: User = Depends(require_roles("student"))):
    """
    A student's personal timetable is a DERIVED VIEW: join enrollments +
    timetable_slots. Not stored as its own table. Joins Subject and the
    teacher's User row too, since the frontend slot-grid needs readable
    names, not raw UUIDs, in each cell.
    """
    subject_ids = [
        row.subject_id for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        ).all()
    ]
    if not subject_ids:
        return []

    teacher = aliased(User)
    rows = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), teacher.full_name.label("teacher_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(teacher, teacher.id == TimetableSlot.teacher_id)
        .filter(TimetableSlot.subject_id.in_(subject_ids), TimetableSlot.deleted_at.is_(None))
        .order_by(TimetableSlot.day_of_week, TimetableSlot.period_number)
        .all()
    )
    return [
        TimetableSlotDetailOut(
            id=slot.id,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_id=slot.teacher_id,
            teacher_name=teacher_name,
            batch_id=slot.batch_id,
            day_of_week=slot.day_of_week,
            period_number=slot.period_number,
            start_time=slot.start_time,
            end_time=slot.end_time,
        )
        for slot, subject_name, teacher_name in rows
    ]


@router.delete("/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_slot(slot_id: uuid.UUID, db: Session = Depends(get_db),
                 current_user: User = Depends(require_roles("admin", "coordinator"))):
    from datetime import datetime, timezone
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id, TimetableSlot.deleted_at.is_(None)).first()
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found")
    slot.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return None
