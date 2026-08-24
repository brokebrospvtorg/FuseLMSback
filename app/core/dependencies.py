from datetime import datetime, timezone, date

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models import User, SystemSettings


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