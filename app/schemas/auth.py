import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.schemas.common import ApprovalStatus, validate_password_strength


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: str
    status: str
    must_change_password: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class TokenVerifyRequest(BaseModel):
    token: str


class TokenVerifyResponse(BaseModel):
    """What the read-only 'Activate Your Account' screen renders."""
    full_name: str
    email: str
    role: str
    token_type: str
    expires_at: datetime


class ActivationSubmitRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)


class CorrectionOnActivationRequest(BaseModel):
    """Used when the pre-filled data shown at activation is wrong."""
    token: str
    requested_changes: dict


class PasswordResetRequestSchema(BaseModel):
    email: EmailStr


class PasswordResetSubmitRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, v: str) -> str:
        return validate_password_strength(v)


class AdminPasswordResetRequestCreate(BaseModel):
    """POST /api/auth/request-password-reset-approval — the logged-out
    'Request Password Reset from Admin' button (login screen). Deliberately
    a free-text identifier rather than EmailStr like PasswordResetRequestSchema
    above: this account may not have email access (that's the whole reason
    for this path instead of the token-email flow), so a Student/Teacher can
    submit their roll number / employee code instead."""
    identifier: str = Field(min_length=1, max_length=255)


class PasswordResetRequestOut(BaseModel):
    """Admin Operations > Password Requests queue row."""
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: Optional[str] = None
    role: Optional[str] = None
    roll_or_employee_id: Optional[str] = None
    identifier_submitted: str
    status: str
    reviewed_by: Optional[uuid.UUID] = None
    review_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class PasswordResetRequestReview(BaseModel):
    """PATCH .../review. 'approved' resets the password to the fixed
    onboarding value and forces a change on next login; 'rejected' just
    closes the request out with no password change."""
    status: ApprovalStatus
    review_note: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    """Self-service change, POST /api/auth/change-password. Requires knowing
    the CURRENT password — this is what distinguishes it from the admin
    reset-password path (which doesn't need it) and from the existing
    forgot-password email-token flow (which doesn't need it either, since
    proving control of the email inbox is the trust anchor there instead)."""
    current_password: str
    new_password: str = Field(min_length=8)
    confirm_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, v: str) -> str:
        return validate_password_strength(v)

    @model_validator(mode="after")
    def passwords_match(self):
        # Validated server-side too, not just in the Angular form — a
        # mismatched confirm field should never reach the point of
        # touching the database, regardless of what the client sent.
        if self.new_password != self.confirm_password:
            raise ValueError("new_password and confirm_password do not match")
        return self
