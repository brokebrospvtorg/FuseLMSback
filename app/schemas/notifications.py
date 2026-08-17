from typing import Optional

from pydantic import BaseModel


class BroadcastNotificationCreate(BaseModel):
    message: str
    role: Optional[str] = None  # None = every active user, regardless of role


class BroadcastResult(BaseModel):
    recipient_count: int
