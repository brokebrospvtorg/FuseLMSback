import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


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


class ChangePasswordRequest(BaseModel):
    """Self-service change, POST /api/auth/change-password. Requires knowing
    the CURRENT password — this is what distinguishes it from the admin
    reset-password path (which doesn't need it) and from the existing
    forgot-password email-token flow (which doesn't need it either, since
    proving control of the email inbox is the trust anchor there instead)."""
    current_password: str
    new_password: str = Field(min_length=8)
    confirm_password: str

    @model_validator(mode="after")
    def passwords_match(self):
        # Validated server-side too, not just in the Angular form — a
        # mismatched confirm field should never reach the point of
        # touching the database, regardless of what the client sent.
        if self.new_password != self.confirm_password:
            raise ValueError("new_password and confirm_password do not match")
        return self
