import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.models import HelpingMaterial, Lecture, Enrollment, User
from app.schemas.content import (
    HelpingMaterialCreate, HelpingMaterialOut, LectureCreate, LectureOut,
)

router = APIRouter(prefix="/api/content", tags=["content"], dependencies=[Depends(check_license)])


def _student_has_subject_access(db: Session, student_id: uuid.UUID, subject_id: uuid.UUID) -> bool:
    """
    ACCESS CONTROL (app-layer, not schema): student sees content if they have
    ANY non-deleted enrollments row for that subject_id (any batch) — so
    students keep access to past-completed subjects' content indefinitely.
    """
    return db.query(Enrollment).filter(
        Enrollment.student_id == student_id, Enrollment.subject_id == subject_id,
        Enrollment.deleted_at.is_(None),
    ).first() is not None


# ---------------------------------------------------------------------------
# Helping materials (subject-scoped, not batch-scoped, reusable across years)
# ---------------------------------------------------------------------------
@router.post("/materials", response_model=HelpingMaterialOut, status_code=status.HTTP_201_CREATED)
def upload_material(payload: HelpingMaterialCreate, db: Session = Depends(get_db),
                     current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    material = HelpingMaterial(**payload.model_dump(), uploaded_by=current_user.id)
    db.add(material)
    db.commit()
    db.refresh(material)
    return material


@router.get("/materials", response_model=List[HelpingMaterialOut])
def list_materials(subject_id: uuid.UUID, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    if current_user.role == "student" and not _student_has_subject_access(db, current_user.id, subject_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this subject")
    return db.query(HelpingMaterial).filter(
        HelpingMaterial.subject_id == subject_id, HelpingMaterial.deleted_at.is_(None)
    ).order_by(HelpingMaterial.uploaded_at.desc()).all()


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def replace_material(material_id: uuid.UUID, db: Session = Depends(get_db),
                      current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    """Materials are 'replaced' via soft-delete + new upload, per the persists/reusable design."""
    material = db.query(HelpingMaterial).filter(
        HelpingMaterial.id == material_id, HelpingMaterial.deleted_at.is_(None)
    ).first()
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material not found")
    material.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Lectures (YouTube unlisted; same batch-independent access rule)
# ---------------------------------------------------------------------------
@router.post("/lectures", response_model=LectureOut, status_code=status.HTTP_201_CREATED)
def upload_lecture(payload: LectureCreate, db: Session = Depends(get_db),
                    current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    lecture = Lecture(**payload.model_dump(), uploaded_by=current_user.id)
    db.add(lecture)
    db.commit()
    db.refresh(lecture)
    return lecture


@router.get("/lectures", response_model=List[LectureOut])
def list_lectures(subject_id: uuid.UUID, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    # KNOWN LIMITATION: "unlisted" gating stops browsing but not a copied link
    # being shared outside the app. Accepted v1 trade-off.
    if current_user.role == "student" and not _student_has_subject_access(db, current_user.id, subject_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this subject")
    return db.query(Lecture).filter(
        Lecture.subject_id == subject_id, Lecture.deleted_at.is_(None)
    ).order_by(Lecture.uploaded_at.desc()).all()
