from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models import User, SystemSettings


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """
    Reads the JWT from the HTTP-Only cookie (never from a header/localStorage),
    validates it, and loads the corresponding active user.
    """
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    user = db.query(User).filter(User.id == payload["sub"], User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")

    return user


def require_roles(*allowed_roles: str):
    """
    Dependency factory implementing server-side RBAC.
    Usage: Depends(require_roles("admin", "coordinator"))
    Frontend visibility is never trusted — every protected router route
    re-checks the role here against the DB-backed current user.
    """

    def _checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not permitted to perform this action",
            )
        return current_user

    return _checker


def check_license(db: Session = Depends(get_db)) -> None:
    """
    SaaS kill-switch. Every core (non-auth) endpoint depends on this.
    If license_expiry_date has passed, all data flow is blocked.
    """
    settings_row = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if not settings_row or not settings_row.license_expiry_date:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="License not configured")

    expiry = settings_row.license_expiry_date
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) > expiry:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="License Expired. Please contact the developer.",
        )
