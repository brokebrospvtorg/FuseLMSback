from typing import Optional

from pydantic import BaseModel, field_validator

from app.schemas.common import Role
from app.utils.sanitize import sanitize_required_text


class BroadcastNotificationCreate(BaseModel):
    message: str
    role: Optional[Role] = None  # None = every active user, regardless of role

    # Stored-XSS defense-in-depth: this message is fanned out and rendered
    # to every recipient's notification feed, so it's a high-value target.
    @field_validator("message")
    @classmethod
    def _sanitize_message(cls, v: str) -> str:
        return sanitize_required_text(v)


class BroadcastResult(BaseModel):
    recipient_count: int
