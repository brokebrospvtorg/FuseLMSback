from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SystemSettingsOut(BaseModel):
    id: int
    license_expiry_date: datetime
    school_name: str
    activated_at: Optional[datetime]

    class Config:
        from_attributes = True


class SystemSettingsUpdate(BaseModel):
    license_expiry_date: Optional[datetime] = None
    school_name: Optional[str] = None
