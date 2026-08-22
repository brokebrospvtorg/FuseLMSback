import uuid
from datetime import date, datetime, time
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.batch_utils import (
    BATCH_SESSIONS, DEFAULT_YEARS_AHEAD, batch_date_range, format_batch_name,
)
from app.schemas.common import BoardEnum


class BatchCreate(BaseModel):
    """
    Only accepts session/year combinations that the standardized Batch
    Generator would actually produce (current year through
    DEFAULT_YEARS_AHEAD years out) — this is what keeps ad-hoc batches from
    being created with a typo'd session, a name that doesn't match
    "<Session> <Year>", or a year far outside the intended window.

    name/start_date/end_date are optional on input: when omitted they're
    derived from session+year via the same utility the seed script and
    frontend dropdown use, so every batch in the system carries an
    identically-formatted name and identical date range for its
    session+year. If provided, they must match the derived values exactly
    (kept as explicit fields, rather than dropped, since BatchOut/the ORM
    still need them and some callers pass them through unchanged).
    """
    session: str  # may_june | oct_nov
    year: int
    name: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    is_current: bool = False
    # schema_update_11: required — every Batch is run under exactly one
    # examining board.
    board: BoardEnum

    @model_validator(mode="after")
    def _validate_against_generator(self) -> "BatchCreate":
        if self.session not in BATCH_SESSIONS:
            raise ValueError(f"session must be one of {BATCH_SESSIONS}")

        current_year = date.today().year
        if not (current_year <= self.year <= current_year + DEFAULT_YEARS_AHEAD):
            raise ValueError(
                f"year must be between {current_year} and {current_year + DEFAULT_YEARS_AHEAD} "
                "(the standard Batch Generator window)"
            )

        expected_name = format_batch_name(self.session, self.year)
        expected_start, expected_end = batch_date_range(self.session, self.year)

        if self.name is None:
            self.name = expected_name
        elif self.name != expected_name:
            raise ValueError(f"name must be '{expected_name}' for session={self.session}, year={self.year}")

        if self.start_date is None:
            self.start_date = expected_start
        elif self.start_date != expected_start:
            raise ValueError(f"start_date must be {expected_start} for session={self.session}, year={self.year}")

        if self.end_date is None:
            self.end_date = expected_end
        elif self.end_date != expected_end:
            raise ValueError(f"end_date must be {expected_end} for session={self.session}, year={self.year}")

        return self


class BatchUpdate(BaseModel):
    """
    PUT /api/academic/batches/{batch_id}. Deliberately narrower than
    BatchCreate: session/year/name/dates are what the Batch Generator
    derives a batch's identity from (see BatchCreate's own docstring) and
    aren't editable here — only `board` is, since that's the one field an
    Admin can get wrong at creation time (or that needs reassigning later)
    with no generator-driven derivation to re-validate against. is_current
    and is_active already have their own dedicated endpoints
    (set-current / set-active) and stay out of this payload so this one
    endpoint has a single, unambiguous purpose.
    """
    board: BoardEnum


class BatchOut(BaseModel):
    id: uuid.UUID
    session: str
    year: int
    name: str
    start_date: date
    end_date: date
    is_current: bool
    board: BoardEnum
    # schema_update_13: "is this batch open for admin work" — see
    # Batch.is_active in models/academic.py for how this differs from
    # is_current above. Defaults true so a bare Batch row (e.g. right after
    # create_batch) reads correctly without the caller setting it explicitly.
    is_active: bool = True
    created_at: datetime
    # Populated by list_batches (routers/academic.py) via grouped
    # subqueries — not stored columns. Default 0 so BatchOut can still be
    # built from a bare Batch row anywhere else it's reused (e.g.
    # create_batch's response) without every caller needing to backfill
    # these first.
    active_students_count: int = 0
    assigned_teachers_count: int = 0
    # schema_update_15: distinct Boards (British Council / Edexcel / LRN)
    # that currently have at least one active BatchSubject offering in
    # this batch. Populated by list_batches via a grouped subquery, same
    # pattern as active_students_count/assigned_teachers_count above — NOT
    # derived from the single Batch.board column, which no longer
    # represents "the" board this batch runs under. Empty list is normal
    # and expected for a batch that hasn't had any subjects offered yet.
    active_boards: list[BoardEnum] = []

    class Config:
        from_attributes = True


class BatchTemplateOut(BaseModel):
    """One entry from the Batch Generator (app/core/batch_utils.py) — a
    session/year combination that SHOULD exist, whether or not a real
    Batch row has been created for it yet. Powers the "which batches am I
    missing" admin dropdown (GET /api/academic/batches/generate)."""
    session: str
    year: int
    name: str
    start_date: date
    end_date: date
    already_exists: bool


class LevelOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    display_order: int
    is_active: bool

    class Config:
        from_attributes = True


class SubjectOut(BaseModel):
    """schema_update_16: `code`, `board`, and `levels` (the full multi-level
    mapping via subject_levels) restored/added. `level_id`/`level_name`
    kept as-is for existing consumers that still key off the single
    primary level (offered-subjects, teacher assignment, enrollment)."""
    id: uuid.UUID
    name: str
    code: str
    board: BoardEnum
    is_active: bool
    level_id: uuid.UUID
    # Joined in by the router for display convenience — not a real column.
    level_name: Optional[str] = None
    # All levels this subject is mapped to via subject_levels, not just
    # the single primary level_id above. Populated by the router.
    levels: List[LevelOut] = Field(default_factory=list)

    class Config:
        from_attributes = True


class SubjectCreate(BaseModel):
    """POST /api/academic/subjects (schema_update_16 — restores the
    create-subject endpoint schema_update_11 deliberately removed)."""
    name: str = Field(..., min_length=1, max_length=200)
    code: str = Field(..., min_length=1, max_length=50)
    board: BoardEnum
    level_ids: List[uuid.UUID] = Field(..., min_length=1)

    @field_validator("name", "code")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("level_ids")
    @classmethod
    def _dedupe_level_ids(cls, v: List[uuid.UUID]) -> List[uuid.UUID]:
        # Preserve order, drop duplicates — a checkbox list can't produce
        # dupes itself, but nothing stops a raw API caller from sending them.
        seen = set()
        deduped = []
        for level_id in v:
            if level_id not in seen:
                seen.add(level_id)
                deduped.append(level_id)
        return deduped


class SubjectUpdate(BaseModel):
    """PUT /api/academic/subjects/{id} — Admin Subjects module. Deliberately
    narrower than SubjectCreate: only name/code are editable here (matches
    the task's "Edit Subject Name/Code" scope). Board and Level mapping are
    catalog-structural decisions made at creation time and aren't exposed
    on this screen — changing them would silently reshape which batches/
    offerings/enrollments this subject applies to, which belongs in a
    separate, more deliberate flow if it's ever needed."""
    name: str = Field(..., min_length=1, max_length=200)
    code: str = Field(..., min_length=1, max_length=50)

    @field_validator("name", "code")
    @classmethod
    def _strip_and_require_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class SubjectStatusUpdate(BaseModel):
    """PATCH /api/academic/subjects/{id}/status — Activate/Deactivate.
    Separate from delete: deactivating hides the subject from every
    "offer this subject" / enrollment / teacher-assignment picker
    (list_subjects filters on is_active) without touching its history,
    and is reversible. Delete is the one-way, dependency-checked action."""
    is_active: bool


class StudentLevelEnrollmentCreate(BaseModel):
    student_id: uuid.UUID
    level_id: uuid.UUID
    started_at: date


class StudentLevelEnrollmentOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    level_id: uuid.UUID
    status: str
    started_at: date
    completed_at: Optional[date]

    class Config:
        from_attributes = True


class SubjectRequestCreate(BaseModel):
    subject_id: uuid.UUID
    batch_id: uuid.UUID


class SubjectRequestReview(BaseModel):
    status: str  # approved | rejected
    # Optional — not persisted as its own column (no migration for this sub-sprint),
    # but folded into the audit log's new_value and the student's notification message.
    comment: Optional[str] = None


class SubjectRequestOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    status: str
    requested_at: datetime
    actioned_by: Optional[uuid.UUID]
    actioned_at: Optional[datetime]

    class Config:
        from_attributes = True


class SubjectRequestReviewRowOut(BaseModel):
    """Coordinator/Admin review-queue shape — same row as SubjectRequestOut,
    joined with display names so the grid doesn't need N+1 lookups client-side."""
    id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    subject_id: uuid.UUID
    subject_name: str
    batch_id: uuid.UUID
    batch_name: str
    status: str
    requested_at: datetime
    actioned_by: Optional[uuid.UUID]
    actioned_at: Optional[datetime]


class StudentEnrollmentRegistrySubject(BaseModel):
    """One row of a student's subject enrollment, joined with display names —
    used by the registry-facing endpoint below."""
    subject_id: uuid.UUID
    subject_name: str
    batch_id: uuid.UUID
    batch_name: str
    status: str
    # Cross-Level Subject Enrollment: the enrollment's own level (Enrollment.level_id),
    # not necessarily the same as the student's current primary level below —
    # this is what lets the registry tag a cross-level row, e.g.
    # "Further Maths [A-Level]" on an otherwise O-Level student.
    level_id: Optional[uuid.UUID] = None
    level_name: Optional[str] = None


class StudentEnrollmentRegistryOut(BaseModel):
    """Registry-display shape for GET /api/academic/student-enrollments/registry —
    a student's current academic level plus their enrolled subjects (active
    batch), joined with display names so the Admin Edit Details view doesn't
    need N+1 lookups client-side. Mirrors TeacherAssignmentRegistryOut's
    role in the Teacher Edit Details view."""
    current_level_id: Optional[uuid.UUID] = None
    current_level_name: Optional[str] = None
    subjects: list[StudentEnrollmentRegistrySubject] = []


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    # Cross-Level Subject Enrollment: the level curriculum this specific
    # enrollment was made under (see Enrollment.level_id in models/academic.py).
    level_id: Optional[uuid.UUID] = None
    status: str
    enrolled_at: datetime

    class Config:
        from_attributes = True


class TeacherSubjectAssignmentCreate(BaseModel):
    teacher_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID


class AssignTeacherToBatchPayload(BaseModel):
    """POST /api/academic/batches/{batch_id}/assign-teacher — batch_id
    comes from the URL path, not the body (unlike TeacherSubjectAssignmentCreate
    above, which is batch-agnostic and used by the generic
    POST /api/academic/teacher-assignments). Both endpoints write the exact
    same TeacherSubjectAssignment row; this one just matches the Admin
    Batches screen's "I'm already working inside this batch" flow."""
    subject_id: uuid.UUID
    teacher_id: uuid.UUID


class OfferSubjectsPayload(BaseModel):
    """POST /api/academic/batches/{batch_id}/offered-subjects. is_active
    defaults True ("offer" is the endpoint's primary purpose per spec) but
    accepts False too, so the same call also handles the Admin Batches
    screen's deactivate action — no separate DELETE/deactivate endpoint
    needed for what's really the same upsert either way.

    schema_update_15: `board` is now required — a batch_subjects row is an
    offering of a subject under a specific examining Board, not just under
    a batch. The same subject_id can be offered more than once for the
    same batch as long as each call uses a different board (e.g. offer
    Physics under both British Council and Edexcel for the same batch).
    """
    subject_ids: list[uuid.UUID]
    board: BoardEnum
    is_active: bool = True


class BatchSubjectOut(BaseModel):
    """One row of GET /api/academic/batches/{batch_id}/offered-subjects —
    joined with display names, same enrichment pattern as SubjectOut."""
    subject_id: uuid.UUID
    subject_name: str
    level_id: uuid.UUID
    level_name: str
    board: BoardEnum
    is_active: bool


class TeacherSubjectAssignmentOut(BaseModel):
    id: uuid.UUID
    teacher_id: uuid.UUID
    subject_id: uuid.UUID
    batch_id: uuid.UUID
    assigned_by: uuid.UUID
    assigned_at: datetime
    # Over-Inclusive Cascading Dropdowns fix: the active BatchSubject board
    # this assignment is actually usable under, resolved server-side from
    # batch_subjects (never inferred from the catalog Subject's own board,
    # which can be "All"). GET /teacher-assignments fans one assignment
    # row out into one row per active board; POST responses pick the
    # (deterministic) first active board at creation time. Always a
    # concrete board (British Council / Edexcel / LRN), never "All".
    board: BoardEnum

    class Config:
        from_attributes = True


class TeacherAssignmentRegistryOut(BaseModel):
    """Registry-display variant of TeacherSubjectAssignmentOut, with subject_name
    and batch_name joined in — added for the Information Registry's "classes
    taught, subjects taught" field (module 2 of the spec). Kept as a separate
    schema/endpoint rather than changing TeacherSubjectAssignmentOut itself,
    since that one is the response shape for POST /teacher-assignments and the
    existing GET list — no reason to force every caller of those to carry
    joined display fields they don't need."""
    subject_id: uuid.UUID
    subject_name: str
    batch_id: uuid.UUID
    batch_name: str


# ---------------------------------------------------------------------------
# Student "me" views — enriched with joined display fields (subject_name,
# teacher_name) so the frontend doesn't need a second round-trip per row.
# ---------------------------------------------------------------------------
class TimetableEntryOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: str
    teacher_name: str
    day_of_week: str
    start_time: time
    end_time: time

    class Config:
        from_attributes = True


class DashboardSummaryOut(BaseModel):
    attendance_percentage: float
    pending_assessments_count: int
    current_batch_name: Optional[str]
    current_batch_year: Optional[int]
    active_subjects_count: int


# ---------------------------------------------------------------------------
# Batch Summary — powers the clickable Batch card/row's inline detail view
# (GET /api/v1/batches/{batch_id}/summary, app/routers/batches.py).
# Admin/Coordinator only.
# ---------------------------------------------------------------------------
class BatchSummaryTeacherOut(BaseModel):
    teacher_id: uuid.UUID
    teacher_name: str
    # Subjects this teacher is assigned to teach WITHIN this batch —
    # lets the drawer show "Ayesha Khan — Physics, Chemistry" per teacher
    # instead of a flat name list.
    subjects: list[str] = []


class BatchSummarySubjectOut(BaseModel):
    subject_id: uuid.UUID
    subject_name: str
    level_name: str  # "O Level" | "A Level"
    # Teachers assigned to this subject within this batch.
    teacher_names: list[str] = []
    # Count of students actively enrolled in this subject within this batch.
    active_student_count: int


class BatchSummaryOut(BaseModel):
    batch_id: uuid.UUID
    batch_name: str
    board: BoardEnum
    is_current: bool
    total_active_students: int
    total_assigned_teachers: int
    teachers: list[BatchSummaryTeacherOut] = []
    # Active subjects & classes assigned to this batch — subjects with zero
    # active enrollments AND zero active teacher assignments in this batch
    # are excluded entirely, per spec ("hide inactive subjects").
    active_subjects: list[BatchSummarySubjectOut] = []
