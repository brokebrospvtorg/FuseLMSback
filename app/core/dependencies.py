from datetime import datetime, timezone, date
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

import uuid

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models import User, SystemSettings, TeacherSubjectAssignment


def coerce_expiry_to_utc_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    raise TypeError(f"Unexpected type for license_expiry_date: {type(value)!r}")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    user = db.query(User).filter(User.id == payload["sub"], User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    
    if user.status not in ("active",) and not (user.status == "pending" and user.must_change_password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")

    return user


def require_roles(*allowed_roles: str):
    def _checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not permitted to perform this action",
            )
        return current_user

    return _checker


def require_teacher_assigned(
    subject_id: uuid.UUID,
    batch_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Object-level authorization (BOLA/IDOR fix): role alone (require_roles)
    was previously sufficient to call marks/content write endpoints for
    ANY subject_id/batch_id pair the caller supplied — a Teacher account
    could enter marks, publish assessments, or upload material for a
    class they were never assigned to just by knowing (or guessing) its
    UUIDs. This checks that a live TeacherSubjectAssignment row actually
    ties this Teacher to this subject (and, when given, this specific
    batch) before letting the request proceed.

    batch_id is optional because not every write path is batch-scoped —
    HelpingMaterial/Lecture uploads only carry subject_id (materials are
    reusable across a subject's batches by design), so those callers omit
    batch_id and this checks "assigned to this subject in ANY batch"
    instead of one exact batch+subject pair.

    Admin/Coordinator bypass entirely — they own the assignment registry
    itself and are not scoped to any single class. Every other role
    (student, parent, or a Teacher with no matching assignment) is
    rejected with 403.

    Usable two ways depending on where subject_id/batch_id live on the
    endpoint:
      - Depends(require_teacher_assigned) when the endpoint already
        declares subject_id (and batch_id) as query or path parameters
        (FastAPI binds this dependency's same-named parameters from the
        request the same way).
      - Called directly, e.g.
        ``require_teacher_assigned(subject_id=payload.subject_id,
        batch_id=payload.batch_id, db=db, current_user=current_user)``,
        when subject_id/batch_id come from a request body, or from a
        parent object (e.g. an Assessment) that must be looked up via a
        path parameter like assessment_id first.
    """
    if current_user.role in ("admin", "coordinator"):
        return current_user

    if current_user.role != "teacher":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user.role}' is not permitted to perform this action",
        )

    query = db.query(TeacherSubjectAssignment).filter(
        TeacherSubjectAssignment.teacher_id == current_user.id,
        TeacherSubjectAssignment.subject_id == subject_id,
        TeacherSubjectAssignment.deleted_at.is_(None),
    )
    if batch_id is not None:
        query = query.filter(TeacherSubjectAssignment.batch_id == batch_id)

    if not query.first():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not assigned to teach this subject"
                   + (" in this batch" if batch_id is not None else ""),
        )

    return current_user


def check_license(db: Session = Depends(get_db)) -> None:
    settings_row = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if not settings_row or not settings_row.license_expiry_date:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="License not configured")

    expiry = coerce_expiry_to_utc_datetime(settings_row.license_expiry_date)

    if datetime.now(timezone.utc) > expiry:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="License Expired. Please contact the developer.",
        )