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
ADMIN_ROLES = frozenset({"admin", "superadmin"})


def is_admin_role(role: str) -> bool:
    """True for any root-level super-user role — never a valid Teacher assignee."""
    return role in ADMIN_ROLES


def guard_teacher_assignee_role(role: str, *, context: str = "a Teacher") -> None:
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
# CSRF token (double-submit cookie pattern)
# ---------------------------------------------------------------------------
# Frontend and backend are on different registrable domains (Vercel /
# Railway), so the frontend can't read this cookie directly via
# document.cookie — it's delivered to the client via the X-CSRF-Token
# response header on /login and /me instead (see routers/auth.py). The
# cookie itself is still what gets validated: on every unsafe request the
# CSRFMiddleware (main.py) checks that the cookie value and the
# X-CSRF-Token request header match. An attacker's cross-site form/fetch
# can make the browser attach the cookie automatically, but has no way to
# read its value to also set the header, so the two won't match.
def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.CSRF_COOKIE_NAME,
        value=token,
        httponly=False,       # must be sendable by the browser as a normal cookie; never read via JS
        secure=True,
        samesite="none",      # same cross-domain requirement as the auth cookie
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.CSRF_COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# Verification tokens (activation / password reset) — random opaque strings
# ---------------------------------------------------------------------------
def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)