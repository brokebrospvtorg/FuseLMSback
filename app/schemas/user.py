import uuid
from datetime import date, datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, EmailStr


# ---------------------------------------------------------------------------
# Users — account creation.
# RBAC (5.1, confirmed): both Admin AND Coordinator can create any non-Admin
# account (student, teacher, coordinator, or parent). Neither can create an
# Admin account through the API — the root Admin is precreated directly in
# the DB at deployment and is never (re)created or reassigned via this
# endpoint. `role` is a Literal (not a free string) specifically so Swagger
# renders it as a fixed dropdown of the 4 valid values instead of a raw text
# box — "admin" isn't one of the options, so an invalid request is rejected
# by Pydantic itself (422) before it ever reaches the route body.
# ---------------------------------------------------------------------------
class UserCreate(BaseModel):
    full_name: str
    email: EmailStr
    role: Literal["coordinator", "teacher", "student", "parent"]
    # Student-only optional fields at creation time:
    roll_number: Optional[str] = None
    admission_date: Optional[date] = None
    # Teacher-only optional fields:
    designation: Optional[str] = None
    hire_date: Optional[date] = None
    # Student-only: link to an existing parent user
    parent_id: Optional[uuid.UUID] = None
    relationship_label: Optional[str] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    status: Optional[str] = None  # active | suspended (Admin/Coordinator — see users.py)
    role: Optional[Literal["coordinator", "teacher", "student", "parent"]] = None
    # 5.3: role reassignment. Same restriction as creation — Admin accounts
    # are never created OR reassigned through the API, so "admin" isn't a
    # valid value here either (Pydantic-level, same as UserCreate.role).


class UserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: str
    status: str
    created_by: Optional[uuid.UUID] = None
    last_login_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class StudentProfileOut(BaseModel):
    user_id: uuid.UUID
    roll_number: Optional[str]
    admission_date: Optional[date]

    class Config:
        from_attributes = True


class TeacherProfileOut(BaseModel):
    user_id: uuid.UUID
    designation: Optional[str]
    hire_date: Optional[date]

    class Config:
        from_attributes = True


class ParentStudentLinkCreate(BaseModel):
    parent_id: uuid.UUID
    student_id: uuid.UUID
    relationship_label: Optional[str] = None


class ParentStudentLinkOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID
    student_id: uuid.UUID
    relationship_label: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Correction requests (student can't self-edit; Admin approves)
# ---------------------------------------------------------------------------
class CorrectionRequestCreate(BaseModel):
    requested_changes: dict


class CorrectionRequestReview(BaseModel):
    status: str  # approved | rejected
    admin_notes: Optional[str] = None


class CorrectionRequestOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    requested_changes: dict
    status: str
    admin_notes: Optional[str] = None
    reviewed_by: Optional[uuid.UUID] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Composite "me" profile — what the student dashboard's Profile card needs.
# NOTE: only covers fields that actually exist in the schema (name, roll
# number, admission date, current class/level). Father's name, CNIC, DOB,
# gender, religion, nationality, and department have no backing columns
# anywhere in the 26-table schema — they are intentionally left off this
# response rather than faked. See chat for the flag on this. ("Marital
# Status" was also dropped from the placeholder list — not applicable for
# school students, and never had a backing column either.)
# ---------------------------------------------------------------------------
class MyProfileOut(BaseModel):
    user: UserOut
    student_profile: Optional[StudentProfileOut] = None
    class_name: Optional[str] = None
