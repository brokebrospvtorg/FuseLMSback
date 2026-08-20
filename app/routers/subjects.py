"""
DEPRECATED (schema_update_11) — NOT mounted in app/main.py anymore.

This router implemented the old "Subject & Class Management" feature:
free-form, batch-scoped subject creation that REQUIRED a subject code.
The updated Academics workflow removes custom subject creation and
subject codes entirely in favor of a pre-declared Cambridge catalog
(see app/seeds/seed_subjects.py, GET /api/academic/subjects) plus
GET /api/v1/batches/{batch_id}/summary (app/routers/batches.py) for the
per-batch "active subjects & classes" view this feature used to cover.

Left in place, unmounted, only so any historical class_subjects rows
remain queryable directly against the DB if ever needed — no HTTP route
in this app reaches this file anymore.
"""
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.core.audit import log_action
from app.models import Batch, ClassSubject, User
from app.schemas.subject import ClassSubjectCreate, ClassSubjectOut, ClassLevelEnum

# NOTE: every other router in this project is mounted unversioned under
# /api/<domain> (e.g. /api/academic, /api/academics — see
# app/routers/academic.py's own docstring on that pair). This one is
# deliberately mounted at /api/v1/subjects per this feature's spec; if the
# rest of the API is ever versioned, this is the router to match against.
router = APIRouter(prefix="/api/v1/subjects", tags=["subjects"], dependencies=[Depends(check_license)])


@router.get("/class-levels", response_model=List[str])
def list_class_levels(current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    The 4 fixed Class Level choices this feature is locked to — powers the
    cascading form's second dropdown. Admin/Coordinator only, same as the
    rest of this router; there's nothing here a Teacher/Student/Parent
    needs, and 403-ing here too (rather than treating it as harmless
    reference data) keeps every endpoint under this prefix consistently
    gated.
    """
    return [level.value for level in ClassLevelEnum]


@router.get("", response_model=List[ClassSubjectOut])
def list_subjects(
    batch_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """Optionally scoped to one Batch (?batch_id=...) — used by the
    "Manage / Add Subjects" view opened from a specific Batch row."""
    query = db.query(ClassSubject, Batch.name.label("batch_name")).join(
        Batch, Batch.id == ClassSubject.batch_id
    )
    if batch_id:
        query = query.filter(ClassSubject.batch_id == batch_id)

    rows = query.order_by(ClassSubject.created_at.desc()).all()
    return [
        ClassSubjectOut(
            id=subject.id,
            name=subject.name,
            code=subject.code,
            class_level=subject.class_level,
            batch_id=subject.batch_id,
            batch_name=batch_name,
            description=subject.description,
            created_at=subject.created_at,
        )
        for subject, batch_name in rows
    ]


@router.post("", response_model=ClassSubjectOut, status_code=status.HTTP_201_CREATED)
def create_subject(
    payload: ClassSubjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Cascading order is enforced here, not just in the frontend form:
    Batch must exist first, then Class Level + Subject Details are
    persisted together in one row. Role check happens before any of that
    — require_roles() 403s a Teacher/Student/Parent before the batch
    lookup even runs.
    """
    batch = db.query(Batch).filter(Batch.id == payload.batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    subject = ClassSubject(
        name=payload.name,
        code=payload.code,
        class_level=payload.class_level.value,
        batch_id=payload.batch_id,
        description=payload.description,
    )
    db.add(subject)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Subject code '{payload.code}' already exists for {batch.name}.",
        )
    db.refresh(subject)

    log_action(
        db, current_user.id, "class_subject_created", "class_subjects", subject.id,
        None,
        {
            "name": subject.name, "code": subject.code,
            "class_level": subject.class_level, "batch_id": str(subject.batch_id),
        },
    )
    db.commit()

    return ClassSubjectOut(
        id=subject.id, name=subject.name, code=subject.code,
        class_level=subject.class_level, batch_id=subject.batch_id,
        batch_name=batch.name, description=subject.description,
        created_at=subject.created_at,
    )
