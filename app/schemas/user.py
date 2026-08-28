import uuid
from datetime import date, datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from app.schemas.common import BoardEnum, GenderEnum, ReligionEnum, NationalityEnum, UserStatus, validate_password_strength


class UserCreate(BaseModel):
    full_name: str
    email: EmailStr
    role: Literal["coordinator", "teacher", "student", "parent"]
    phone_number: Optional[str] = Field(
        default=None, pattern=r"^(\+92|0)3\d{9}$",
        description="Pakistani mobile format: 03XXXXXXXXX or +923XXXXXXXXX",
    )
    admission_date: Optional[date] = None
    father_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[GenderEnum] = None
    religion: Optional[ReligionEnum] = None
    nationality: Optional[NationalityEnum] = None
    cnic: Optional[str] = Field(
        default=None, pattern=r"^\d{5}-\d{7}-\d{1}$",
        description="Pakistani CNIC format: XXXXX-XXXXXXX-X",
    )
    registration_id: Optional[str] = None  # Added back to prevent frontend mismatch errors
    board: Optional[BoardEnum] = None
    hire_date: Optional[date] = None
    boards: Optional[List[BoardEnum]] = None
    level_ids: Optional[List[uuid.UUID]] = None
    parent_link_mode: Optional[Literal["existing", "later"]] = None
    parent_id: Optional[uuid.UUID] = None
    relationship_label: Optional[str] = None
    batch_id: Optional[uuid.UUID] = None
    level_id: Optional[uuid.UUID] = None  
    subject_ids: Optional[List[uuid.UUID]] = None
    initial_password: Optional[str] = Field(default=None, min_length=8)

    @field_validator("initial_password")
    @classmethod
    def _validate_initial_password(cls, v: Optional[str]) -> Optional[str]:
        # Optional: a caller (Admin/Coordinator) may omit it entirely and
        # let the forced-default/onboarding flow apply instead — see
        # users.py's effective_initial_password branch. Only validate
        # complexity when a value was actually supplied.
        if v is None:
            return v
        return validate_password_strength(v)

    @field_validator("level_ids")
    @classmethod
    def _dedupe_level_ids(cls, v: Optional[List[uuid.UUID]]) -> Optional[List[uuid.UUID]]:
        if v is None:
            return v
        seen: dict[uuid.UUID, None] = {}
        for level_id in v:
            seen.setdefault(level_id, None)
        return list(seen.keys())

    @model_validator(mode="after")
    def _require_board_for_role(self) -> "UserCreate":
        if self.role == "student" and self.board is None:
            raise ValueError("board is required when creating a student")
        if self.role == "teacher" and not self.boards:
            raise ValueError("boards must include at least one board when creating a teacher")
        if self.role == "teacher" and not self.level_ids:
            raise ValueError("level_ids must include at least one level when creating a teacher")
        return self


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    status: Optional[UserStatus] = None
    role: Optional[Literal["coordinator", "teacher", "student", "parent"]] = None
    phone_number: Optional[str] = Field(
        default=None, pattern=r"^(\+92|0)3\d{9}$",
    )
    roll_number: Optional[str] = None
    admission_date: Optional[date] = None
    father_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[GenderEnum] = None
    religion: Optional[ReligionEnum] = None
    nationality: Optional[NationalityEnum] = None
    cnic: Optional[str] = Field(
        default=None, pattern=r"^\d{5}-\d{7}-\d{1}$",
    )
    registration_id: Optional[str] = None  
    board: Optional[BoardEnum] = None
    designation: Optional[str] = None  
    hire_date: Optional[date] = None
    teacher_code: Optional[str] = None
    boards: Optional[List[BoardEnum]] = None
    level_ids: Optional[List[uuid.UUID]] = None
    level_id: Optional[uuid.UUID] = None  
    batch_id: Optional[uuid.UUID] = None
    subject_ids: Optional[List[uuid.UUID]] = None

    @field_validator("level_ids")
    @classmethod
    def _level_ids_not_empty_when_provided(cls, v: Optional[List[uuid.UUID]]) -> Optional[List[uuid.UUID]]:
        if v is not None and len(v) == 0:
            raise ValueError("level_ids cannot be empty — a teacher must teach at least one level")
        seen: dict[uuid.UUID, None] = {}
        for level_id in v or []:
            seen.setdefault(level_id, None)
        return list(seen.keys()) if v is not None else v

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
    gender: Optional[GenderEnum] = None
    religion: Optional[ReligionEnum] = None
    nationality: Optional[NationalityEnum] = None
    cnic: Optional[str] = None
    registration_id: Optional[str] = None
    board: BoardEnum

    class Config:
        from_attributes = True


class TeacherProfileOut(BaseModel):
    user_id: uuid.UUID
    hire_date: Optional[date]
    gender: Optional[str] = None
    cnic: Optional[str] = None
    teacher_code: Optional[str] = None
    boards: List[BoardEnum] = []
    level_ids: List[uuid.UUID] = []

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
    student_id: uuid.UUID
    full_name: str
    roll_number: Optional[str] = None
    relationship: Optional[str] = None


class CorrectionRequestCreate(BaseModel):
    requested_changes: dict


class CorrectionRequestReview(BaseModel):
    status: str
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


class MyProfileOut(BaseModel):
    user: UserOut
    student_profile: Optional[StudentProfileOut] = None
    teacher_profile: Optional[TeacherProfileOut] = None
    parent_profile: Optional[ParentProfileOut] = None
    class_name: Optional[str] = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8)

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, v: str) -> str:
        return validate_password_strength(v)