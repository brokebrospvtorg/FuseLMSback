import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: str
    status: str
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
