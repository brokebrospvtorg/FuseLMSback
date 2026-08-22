import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Response, status
from jose import jwt, JWTError
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# Admin Role Isolation Guard
# ---------------------------------------------------------------------------
# Accounts with role 'admin' (or 'superadmin' — not yet a live UserRole enum
# value, guarded pre-emptively so this doesn't quietly regress the day it's
# added) are strictly root-level super-users/system management identities.
# They must never be assignable as a Teacher anywhere a teacher_id is
# accepted — TeacherSubjectAssignment (app/routers/academic.py) and
# TimetableSlot (app/routers/timetable.py) both accept a teacher_id and are
# the two places this has to be enforced. This lives in security.py (rather
# than duplicated inline in each router) so every call site shares one
# definition of "who counts as an admin" and one error message.
ADMIN_ROLES = frozenset({"admin", "superadmin"})


def is_admin_role(role: str) -> bool:
    """True for any root-level super-user role — never a valid Teacher assignee."""
    return role in ADMIN_ROLES


def guard_teacher_assignee_role(role: str, *, context: str = "a Teacher") -> None:
    """
    Root Role Isolation Guard: raises 400 if `role` (the role of the user
    being assigned) is an admin-tier role. Call this at every write path
    that accepts a teacher_id/teacher_user_id — before the row is created
    or updated, never after — so an Admin/Superadmin account can never end
    up attached to a Batch, Subject, or Timetable Slot as the teacher.

    This is a defense-in-depth check: some call sites additionally query
    for teacher_id scoped to role == 'teacher' (or a Coordinator with a
    legacy TeacherProfile) and would already 404 an admin's id via that
    filter alone. This guard exists so the rejection is explicit,
    intentional, and produces a clear 400 rather than relying solely on an
    incidental side effect of an unrelated eligibility filter.
    """
    if is_admin_role(role):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Accounts with the 'admin' or 'superadmin' role cannot be assigned as {context}. "
                "Admins are strictly super-users/root management, not teaching staff."
            ),
        )


# ---------------------------------------------------------------------------
# Password hashing (Bcrypt)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


# ---------------------------------------------------------------------------
# JWT access tokens (short expiry, delivered via HTTP-Only cookie)
# ---------------------------------------------------------------------------
def create_access_token(user_id: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "role": role, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,          # was: settings.COOKIE_SECURE — force True; SameSite=None requires it
        samesite="none",      # was: "strict" — required cross-domain (vercel.app ↔ railway.app)
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# Verification tokens (activation / password reset) — random opaque strings
# ---------------------------------------------------------------------------
def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)
