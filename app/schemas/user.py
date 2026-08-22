import uuid
from datetime import date, datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.schemas.common import BoardEnum


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
    # Student-only optional fields at creation time.
    # Auto Roll Number: roll_number is NOT accepted here at all — it's
    # always server-generated (format INK-{year}-XXXX, see
    # users.py:_next_roll_number) the moment a student_profiles row is
    # created, mirroring the existing Teacher Code convention exactly.
    # (UserUpdate still has a roll_number field further down — that's the
    # Edit Details/correction path for an already-existing student.)
    admission_date: Optional[date] = None
    father_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    religion: Optional[str] = None
    nationality: Optional[str] = None
    cnic: Optional[str] = None
    # Student-only: the student's own exam-board registration id (unrelated
    # to a Parent's Reg ID below). Still caller-supplied — Auto Roll Number
    # only covers roll_number, not this field.
    registration_id: Optional[str] = None
    # Student-only, REQUIRED when role == "student" (validated below): the
    # exam board this student is registered under. Optional at the schema
    # level (same convention as every other role-specific field on this
    # shared payload) so a Teacher/Parent/Coordinator payload isn't forced
    # to carry a meaningless board value.
    board: Optional[BoardEnum] = None
    # Teacher-only optional fields (gender/cnic reused above, shared shape):
    designation: Optional[str] = None
    hire_date: Optional[date] = None
    # Admin Teacher Creation: Teacher Code is NOT accepted here at all —
    # it's always server-generated (format INK-T-XXXX, see
    # users.py:_next_teacher_code) the moment a teacher_profiles row is
    # created, so there's deliberately no field on this schema for a
    # caller to set it manually. (UserUpdate still has a teacher_code
    # field further down — that's the Edit Details/correction path for an
    # already-existing teacher, out of scope for this change.)
    # Teacher-only, REQUIRED (at least one) when role == "teacher": the
    # board(s) this teacher is qualified to teach.
    boards: Optional[List[BoardEnum]] = None
    # ------------------------------------------------------------------
    # Parent Link Flow (Student creation only): the Admin explicitly picks
    # one of two paths in the UI — "Link Existing Parent" (parent_id
    # required) or "Link Later" (parent_id omitted, no link created now;
    # Link Parent from the Registry row action covers it afterwards). This
    # field exists purely so a mis-wired frontend (parent_link_mode ==
    # "existing" with no parent_id) is caught as a clean 422/400 instead of
    # silently creating a student with no parent link and no error.
    # ------------------------------------------------------------------
    parent_link_mode: Optional[Literal["existing", "later"]] = None
    parent_id: Optional[uuid.UUID] = None
    relationship_label: Optional[str] = None
    # ------------------------------------------------------------------
    # Cascading Scope (Student creation only): Batch -> Level -> Subject,
    # same three-stage shape as the existing Add Teacher initial-assignment
    # cascade (newTeacherBatchId/newTeacherLevelId/newTeacherSubjectIds on
    # the frontend). All optional — leaving batch_id unset creates the
    # Student with no initial enrollment, exactly like today; Admin User
    # Management (PATCH .../users/{id}) remains available afterwards
    # either way. subject_ids requires batch_id + level_id to be
    # meaningful (enforced below and in the router, same "validate before
    # writing" pattern as update_user's level/subject block).
    # ------------------------------------------------------------------
    batch_id: Optional[uuid.UUID] = None
    level_id: Optional[uuid.UUID] = None
    subject_ids: Optional[List[uuid.UUID]] = None
    # Optional: Admin/Coordinator sets the account's first password directly
    # instead of the normal email-activation flow. When provided, the new
    # user is created status='active' immediately (skips the pending/token
    # step entirely) with must_change_password=True. When omitted (the
    # default, unchanged from before this sprint), account creation works
    # exactly as it did — status='pending', activation email sent, user
    # picks their own first password. Ignored entirely for role ==
    # "student" (see DEFAULT_STUDENT_INITIAL_PASSWORD server-side, which
    # always wins) and role == "teacher" (DEFAULT_TEACHER_INITIAL_PASSWORD).
    initial_password: Optional[str] = Field(default=None, min_length=8)

    @field_validator("boards")
    @classmethod
    def _dedupe_boards(cls, v: Optional[List[BoardEnum]]) -> Optional[List[BoardEnum]]:
        if v is None:
            return v
        # Preserve first-seen order while dropping duplicates — a teacher
        # multi-select shouldn't be able to submit the same board twice.
        seen: dict[BoardEnum, None] = {}
        for board in v:
            seen.setdefault(board, None)
        return list(seen.keys())

    @field_validator("subject_ids")
    @classmethod
    def _dedupe_subject_ids(cls, v: Optional[List[uuid.UUID]]) -> Optional[List[uuid.UUID]]:
        if v is None:
            return v
        seen: dict[uuid.UUID, None] = {}
        for subject_id in v:
            seen.setdefault(subject_id, None)
        return list(seen.keys())

    @model_validator(mode="after")
    def _require_board_for_role(self) -> "UserCreate":
        if self.role == "student" and self.board is None:
            raise ValueError("board is required when creating a student")
        if self.role == "teacher" and not self.boards:
            raise ValueError("boards must include at least one board when creating a teacher")
        return self

    @model_validator(mode="after")
    def _validate_parent_link_flow(self) -> "UserCreate":
        # Only meaningful for role == "student" — silently ignored (not
        # errored) for every other role, same "role-agnostic at the schema
        # level" convention as the rest of this shared payload.
        if self.role == "student" and self.parent_link_mode == "existing" and self.parent_id is None:
            raise ValueError('parent_id is required when parent_link_mode is "existing"')
        return self

    @model_validator(mode="after")
    def _validate_cascading_scope(self) -> "UserCreate":
        # Shape-level check only (does the Batch/Level actually exist, is
        # each subject_id real) happens in the router, same as the rest of
        # this cascade's validation — this just guards against a Subject
        # pick with no Batch/Level underneath it ever reaching the DB layer.
        if self.role == "student" and self.subject_ids:
            if self.batch_id is None:
                raise ValueError("batch_id is required when subject_ids is provided")
            if self.level_id is None:
                raise ValueError("level_id is required when subject_ids is provided")
        return self


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
    # Student-only: exam board (schema_update_11). Omit to leave unchanged;
    # the Student edit form always sends it since it's a required field on
    # that form, but it stays Optional here so a PATCH touching only other
    # fields (e.g. suspend/reactivate) doesn't have to resend it.
    board: Optional[BoardEnum] = None
    designation: Optional[str] = None
    hire_date: Optional[date] = None
    teacher_code: Optional[str] = None
    # Teacher-only: full replacement of the boards this teacher is
    # qualified to teach (schema_update_11) — send the complete desired
    # list, not a delta, same convention as `subject_ids` below. An empty
    # list is invalid for a teacher (a teacher must be qualified for at
    # least one board); omit the field entirely to leave it unchanged.
    boards: Optional[List[BoardEnum]] = None
    # Student academic level + subject assignment (Admin User Management).
    # Both optional/role-agnostic at the schema level, same convention as the
    # rest of this class — users.py only applies them when the target user
    # is a student, and silently ignores them otherwise. `subject_ids` is a
    # full replacement of the student's active subject set for the current
    # batch (send the complete desired list, not a delta) — an empty list is
    # a valid, explicit "unassign every subject", distinct from omitting the
    # field entirely (which leaves subjects untouched).
    level_id: Optional[uuid.UUID] = None
    # Batch -> Level -> Subject cascade (Registry Cascading Dropdowns): the
    # Batch that level_id/subject_ids resolve against, now an explicit,
    # caller-supplied field. Previously omitted entirely from this schema —
    # the router always resolved "the" batch itself via Batch.is_current,
    # so a Student's subject enrollment could only ever be managed against
    # whichever batch happened to be flagged current, never any other one.
    # Optional/role-agnostic here, same convention as level_id/subject_ids;
    # omit to leave existing batch-scoped enrollments untouched.
    batch_id: Optional[uuid.UUID] = None
    subject_ids: Optional[List[uuid.UUID]] = None

    @field_validator("boards")
    @classmethod
    def _boards_not_empty_when_provided(cls, v: Optional[List[BoardEnum]]) -> Optional[List[BoardEnum]]:
        if v is not None and len(v) == 0:
            raise ValueError("boards cannot be empty — a teacher must be qualified for at least one board")
        seen: dict[BoardEnum, None] = {}
        for board in v or []:
            seen.setdefault(board, None)
        return list(seen.keys()) if v is not None else v


class UserOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    role: str
    status: str
    must_change_password: bool
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
    board: BoardEnum

    class Config:
        from_attributes = True


class TeacherProfileOut(BaseModel):
    user_id: uuid.UUID
    designation: Optional[str]
    hire_date: Optional[date]
    gender: Optional[str] = None
    cnic: Optional[str] = None
    teacher_code: Optional[str] = None
    # Populated by the router from TeacherBoard rows — not a real column on
    # teacher_profiles (see app/models/user.py: TeacherProfile.boards is a
    # relationship, not a Column), so this can't just be from_attributes'd
    # off the plain ORM object without the router mapping it in.
    boards: List[BoardEnum] = []

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


class AdminResetPasswordRequest(BaseModel):
    """POST /api/users/{user_id}/reset-password — Admin/Coordinator sets a
    new (temporary) password for someone else. No current_password needed
    here, unlike ChangePasswordRequest — the caller's own admin/coordinator
    session is the trust anchor, not knowledge of the target's old password.
    Always sets must_change_password=True on the target (see router)."""
    new_password: str = Field(min_length=8)
