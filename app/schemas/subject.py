import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ClassLevelEnum(str, Enum):
    """
    Mirrors the `class_level` Postgres enum (schema_update_10_class_subjects.sql)
    and the frontend's ClassLevel enum (src/app/core/models/enums.ts) — keep
    all three in sync by hand, same convention as every other enum in this
    project (see the note atop app/models/enums.py).
    """
    O_LEVEL = "O Level"
    AS_LEVEL = "AS Level"
    A2_LEVEL = "A2 Level"
    A_LEVEL_COMBINED = "A Level (Combined)"


class ClassSubjectCreate(BaseModel):
    batch_id: uuid.UUID
    class_level: ClassLevelEnum
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=20)
    description: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("name", "code")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class ClassSubjectOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    class_level: ClassLevelEnum
    batch_id: uuid.UUID
    # Joined in by the router for display — not a real column on the row.
    batch_name: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
