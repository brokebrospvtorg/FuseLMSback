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
    phone_number: Optional[str] = None
    # Student-only optional fields at creation time:
    roll_number: Optional[str] = None
    admission_date: Optional[date] = None
    father_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    religion: Optional[str] = None
    nationality: Optional[str] = None
    cnic: Optional[str] = None
    registration_id: Optional[str] = None
    # Teacher-only optional fields (gender/cnic reused above, shared shape):
    designation: Optional[str] = None
    hire_date: Optional[date] = None
    teacher_code: Optional[str] = None
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
    phone_number: Optional[str] = None
    # Registry-detail editing (Coordinator/Admin, per spec module 2). All
    # optional and role-agnostic at the schema level — users.py only writes
    # the ones that actually apply to the target user's current role, and
    # silently ignores the rest rather than erroring, so the same PATCH
    # body works regardless of which profile type is on the other end.
    roll_number: Optional[str] = None
    admission_date: Optional[date] = None
    father_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    religion: Optional[str] = None
    nationality: Optional[str] = None
    cnic: Optional[str] = None
    registration_id: Optional[str] = None
    designation: Optional[str] = None
    hire_date: Optional[date] = None
    teacher_code: Optional[str] = None


class UserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: str
    status: str
    phone_number: Optional[str] = None
    created_by: Optional[uuid.UUID] = None
    last_login_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class StudentProfileOut(BaseModel):
    user_id: uuid.UUID
    roll_number: Optional[str]
    admission_date: Optional[date]
    father_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    religion: Optional[str] = None
    nationality: Optional[str] = None
    cnic: Optional[str] = None
    registration_id: Optional[str] = None

    class Config:
        from_attributes = True


class TeacherProfileOut(BaseModel):
    user_id: uuid.UUID
    designation: Optional[str]
    hire_date: Optional[date]
    gender: Optional[str] = None
    cnic: Optional[str] = None
    teacher_code: Optional[str] = None

    class Config:
        from_attributes = True


class ParentProfileOut(BaseModel):
    user_id: uuid.UUID
    cnic: Optional[str] = None
    registration_id: Optional[str] = None
    registration_date: Optional[date] = None

    class Config:
        from_attributes = True


class UserDetailOut(UserOut):
    """UserOut + whichever profile table applies to this user's role — this
    is what the Registry's "Edit Details" screen fetches so it has every
    editable field in one call instead of the list summary alone."""
    student_profile: Optional[StudentProfileOut] = None
    teacher_profile: Optional[TeacherProfileOut] = None
    parent_profile: Optional[ParentProfileOut] = None


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


class ParentChildRegistryOut(BaseModel):
    """Registry-display shape for GET /api/users/{parent_id}/children —
    same fields as parent.py's ParentChildOut (the parent-self-facing
    version), duplicated here rather than imported across router modules
    to keep users.py's schema surface self-contained."""
    student_id: uuid.UUID
    full_name: str
    roll_number: Optional[str] = None
    relationship: Optional[str] = None


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
# UPDATE (schema_update.sql applied): father's name, DOB, gender, religion,
# nationality, and CNIC now DO have backing columns (student_profiles /
# teacher_profiles / parent_profiles) — the "intentionally left off, no
# backing column" note from before this migration no longer applies.
# "Department" was never a real field for anyone and stays off deliberately
# — it was a frontend-only placeholder that should be removed, not wired up.
# ---------------------------------------------------------------------------
class MyProfileOut(BaseModel):
    user: UserOut
    student_profile: Optional[StudentProfileOut] = None
    teacher_profile: Optional[TeacherProfileOut] = None
    parent_profile: Optional[ParentProfileOut] = None
    class_name: Optional[str] = None
