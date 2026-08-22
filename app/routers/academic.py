import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, distinct
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.audit import log_action
from app.core.notifications import notify
from app.core.batch_utils import generate_batches
from app.core.offering_utils import active_boards_for, active_boards_map
from app.core.security import guard_teacher_assignee_role
from sqlalchemy import and_, or_

from app.models import (
    Batch, Level, Subject, SubjectLevel, StudentLevelEnrollment, SubjectRequest, Enrollment,
    TeacherSubjectAssignment, BatchSubject, User, TimetableSlot, AttendanceRecord, Assessment, Mark,
    TeacherProfile,
)
from app.schemas.academic import (
    BatchCreate, BatchUpdate, BatchOut, BatchTemplateOut, LevelOut, SubjectOut, SubjectCreate,
    StudentLevelEnrollmentCreate, StudentLevelEnrollmentOut,
    SubjectRequestCreate, SubjectRequestReview, SubjectRequestOut, EnrollmentOut,
    TeacherSubjectAssignmentCreate, TeacherSubjectAssignmentOut, TeacherAssignmentRegistryOut,
    TimetableEntryOut, DashboardSummaryOut, SubjectRequestReviewRowOut,
    StudentEnrollmentRegistryOut, StudentEnrollmentRegistrySubject,
    AssignTeacherToBatchPayload, OfferSubjectsPayload, BatchSubjectOut,
)

# schema_update_16: the exact 4 standardized level codes — GET /levels
# filters to these (belt-and-braces alongside is_active) so a row that's
# active but was never cleaned up to one of the 4 canonical codes still
# can't leak into the dropdown.
STANDARD_LEVEL_CODES = ("O-LEVEL", "AS-LEVEL", "A2-LEVEL", "A-LEVEL")

router = APIRouter(prefix="/api/academic", tags=["academic"], dependencies=[Depends(check_license)])


# ---------------------------------------------------------------------------
# Batches — Admin/Coordinator manage; only one is_current=true (DB-enforced)
# ---------------------------------------------------------------------------
@router.post("/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db),
                  current_user: User = Depends(require_roles("admin", "coordinator"))):
    if payload.is_current:
        db.query(Batch).filter(Batch.is_current.is_(True)).update({"is_current": False})
    data = payload.model_dump()
    data["board"] = payload.board.value  # BoardEnum -> plain str for the Postgres enum column
    batch = Batch(**data)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=List[BatchOut])
def list_batches(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    active_students_count / assigned_teachers_count: same filter
    conditions as the per-batch GET /{batch_id}/summary endpoint in
    routers/batches.py (Enrollment.status == "active", deleted_at IS NULL
    on both tables) — kept consistent so a batch that looks "active" here
    matches what its own summary drawer shows. Computed as two grouped
    subqueries (one round-trip each, not one query per batch) rather than
    a per-batch loop, which is what made the frontend's earlier attempt at
    this reach for undefined fields (active_students_count didn't exist on
    BatchOut at all) instead of a real fix.

    Ordering (Coordinator Portal batch-ordering requirement): a strict
    2-tier priority —
      1. is_active == True — ACTIVE batches always come first, sorted
         CHRONOLOGICALLY (year ASC, then start_date ASC as the same-year
         tiebreaker) e.g. "2025 Active" before "2026 Active".
      2. is_active == False — inactive/completed batches are always pushed
         to the bottom, below every active batch regardless of year, also
         chronological (year ASC, start_date ASC) within that group.

    Deliberately NOT weighted by active_students_count / active_boards —
    an earlier revision sorted active batches by "how much is happening in
    them" (most-enrolled first) with year only as a tiebreaker. That
    produced an order Coordinators found confusing/unpredictable (a
    newer batch could jump above an older one just because it had more
    enrollments), and doesn't match this requirement's explicit
    chronological ordering. active_students_count/assigned_teachers_count/
    active_boards are still computed and returned on every row (the Batch
    Summary drawer and per-board "Active" tag both need them) — they're
    just no longer part of the sort key.

    This is computed in Python over the already-fetched, already-decorated
    BatchOut list rather than as a SQL ORDER BY, so it stays in one place
    and is trivial to unit test against the BatchOut list directly.

    Every board tab on the frontend (All Batches, British Council, Edexcel,
    LRN) filters this same already-ordered list rather than re-querying or
    re-sorting, so all four inherit this exact ordering for free — see
    admin-batches.component.ts's filteredBatches computed signal, which is
    a deliberate pass-through of this order, never a re-sort.
    """
    student_counts = dict(
        db.query(Enrollment.batch_id, func.count(distinct(Enrollment.student_id)))
        .filter(Enrollment.status == "active", Enrollment.deleted_at.is_(None))
        .group_by(Enrollment.batch_id)
        .all()
    )
    teacher_counts = dict(
        db.query(TeacherSubjectAssignment.batch_id, func.count(distinct(TeacherSubjectAssignment.teacher_id)))
        .filter(TeacherSubjectAssignment.deleted_at.is_(None))
        .group_by(TeacherSubjectAssignment.batch_id)
        .all()
    )

    # schema_update_15: which Boards each batch currently has ACTIVE
    # offered-subject rows under — a Batch can span all three at once, so
    # this is a batch_id -> list[board] map, not a single value. Joined
    # against Subject so a soft-deleted subject's board doesn't count.
    active_boards_map: dict = {}
    board_rows = (
        db.query(BatchSubject.batch_id, BatchSubject.board)
        .join(Subject, Subject.id == BatchSubject.subject_id)
        .filter(BatchSubject.is_active.is_(True), Subject.deleted_at.is_(None))
        .distinct()
        .all()
    )
    for batch_id, board in board_rows:
        active_boards_map.setdefault(batch_id, []).append(board)

    batches = db.query(Batch).filter(Batch.deleted_at.is_(None)).all()

    result = []
    for batch in batches:
        out = BatchOut.model_validate(batch)
        out.active_students_count = student_counts.get(batch.id, 0)
        out.assigned_teachers_count = teacher_counts.get(batch.id, 0)
        out.active_boards = active_boards_map.get(batch.id, [])
        result.append(out)

    result.sort(
        key=lambda b: (
            0 if b.is_active else 1,  # ACTIVE batches first, inactive always last
            b.year,                   # chronological ASC within each group (2025 before 2026)
            b.start_date,             # same-year tiebreaker, also chronological
        )
    )
    return result


@router.get("/batches/generate", response_model=List[BatchTemplateOut])
def generate_batch_templates(db: Session = Depends(get_db),
                              current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    The standardized Batch Generator (app/core/batch_utils.py): current
    year through 4 years ahead, May/June + Oct/Nov every year — flagged
    with whether a real (non-deleted) Batch row already exists for each
    one. This is what the frontend's "Create Batch" dropdown sources its
    options from, instead of letting an Admin type a free-text session/
    year (see BatchCreate's own validation for the server-side half of
    that same guarantee).
    """
    existing = {
        (b.session, b.year)
        for b in db.query(Batch.session, Batch.year).filter(Batch.deleted_at.is_(None)).all()
    }
    return [
        BatchTemplateOut(
            session=t.session, year=t.year, name=t.name,
            start_date=t.start_date, end_date=t.end_date,
            already_exists=(t.session, t.year) in existing,
        )
        for t in generate_batches()
    ]


@router.put("/batches/{batch_id}", response_model=BatchOut)
def update_batch(batch_id: uuid.UUID, payload: BatchUpdate, db: Session = Depends(get_db),
                  current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Currently the only editable field is `board` (see BatchUpdate's own
    docstring for why session/year/dates/is_current/is_active aren't
    here). Fixes the real gap this endpoint exists to close: batches
    created before an Admin picked the right board — or that need
    reassigning later — had no way to change board after creation, which
    is why every batch could get permanently stuck under one Board tab
    with no path off it.
    """
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    old_board = batch.board
    batch.board = payload.board.value
    log_action(
        db, current_user.id, "batch_board_updated", "batches", batch.id,
        {"board": old_board}, {"board": batch.board},
    )
    db.commit()
    db.refresh(batch)
    return batch


@router.patch("/batches/{batch_id}/set-current", response_model=BatchOut)
def set_current_batch(batch_id: uuid.UUID, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("admin", "coordinator"))):
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    db.query(Batch).filter(Batch.is_current.is_(True)).update({"is_current": False})
    batch.is_current = True
    db.commit()
    db.refresh(batch)
    return batch


@router.patch("/batches/{batch_id}/set-active", response_model=BatchOut)
def set_batch_active(batch_id: uuid.UUID, is_active: bool, db: Session = Depends(get_db),
                      current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    schema_update_13: toggles Batch.is_active — "is this batch open for
    admin work" (assigning teachers, offering subjects, taking subject
    requests), independent of is_current above. Unlike set-current, more
    than one batch can be active at once, so this is a plain boolean flip
    on the one batch, not a single-winner reassignment.
    """
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    batch.is_active = is_active
    db.commit()
    db.refresh(batch)
    return batch


# ---------------------------------------------------------------------------
# Offered Subjects per Batch (schema_update_13) — Admin pre-declares which
# subjects are actually running in a batch; Student/Parent subject-request
# screens read ONLY from this, never the raw subject catalog, so nothing
# shows up as requestable until Admin has explicitly offered it.
# ---------------------------------------------------------------------------
@router.get("/batches/{batch_id}/offered-subjects", response_model=List[BatchSubjectOut])
def list_offered_subjects(batch_id: uuid.UUID, board: Optional[str] = None,
                           db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    """
    Any authenticated role can call this (Student/Parent need it for
    subject requests, Admin/Coordinator for the Batches management screen,
    Teacher for context) — it only ever returns subjects already marked
    is_active = True, so there's nothing sensitive to gate by role here.
    The Admin "which subjects are NOT yet offered, to pick from" view is
    just this response diffed against GET /subjects?level_id= on the
    frontend — no separate "list inactive too" endpoint needed.

    schema_update_15: optional `board` query param narrows to offerings
    under one examining Board — the same batch can have the same subject
    offered under more than one board, so callers that care about a
    specific Board Tab (admin-batches.component.ts) should pass this
    rather than filtering the unfiltered list client-side.
    """
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    query = (
        db.query(BatchSubject, Subject.name.label("subject_name"),
                  SubjectLevel.level_id.label("level_id"), Level.name.label("level_name"))
        .join(Subject, Subject.id == BatchSubject.subject_id)
        # BUG FIX: was joined via Subject.level_id (the single "primary"
        # level FK) — a multi-level subject (e.g. Physics offered under
        # both O-Level and AS-Level, see SubjectLevel/schema_update_16b)
        # would then only ever be reported under whichever level happened
        # to be its primary one, making it invisible — and therefore
        # un-assignable to a Teacher — in ManageBatchDialogComponent's
        # Assign Teacher list for its other level(s), even after being
        # successfully offered. subject_levels has one row per (subject,
        # applicable level) pair for EVERY subject (schema_update_16b
        # backfilled it for single-level subjects too), so joining through
        # it here — instead of the single FK — naturally returns one row
        # per level a multi-level subject actually belongs to, with
        # single-level subjects behaving exactly as before (one row).
        .join(SubjectLevel, SubjectLevel.subject_id == Subject.id)
        .join(Level, Level.id == SubjectLevel.level_id)
        .filter(
            BatchSubject.batch_id == batch_id,
            BatchSubject.is_active.is_(True),
            Subject.deleted_at.is_(None),
        )
    )
    if board:
        query = query.filter(BatchSubject.board == board)

    rows = query.order_by(Level.display_order, Subject.name).all()
    return [
        BatchSubjectOut(
            subject_id=bs.subject_id, subject_name=subject_name,
            level_id=level_id, level_name=level_name,
            board=bs.board, is_active=bs.is_active,
        )
        for bs, subject_name, level_id, level_name in rows
    ]


@router.post("/batches/{batch_id}/offered-subjects", response_model=List[BatchSubjectOut])
def offer_subjects_for_batch(batch_id: uuid.UUID, payload: OfferSubjectsPayload, db: Session = Depends(get_db),
                              current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Upserts a batch_subjects row per (subject_id, board) — activating (or,
    with is_active=False, deactivating) it for this batch. Same endpoint
    handles both directions of the Admin Batches screen's
    "activate/deactivate subjects per batch" toggle (spec section 4)
    rather than needing a separate deactivate/DELETE route.

    schema_update_15: upsert key is now (batch_id, subject_id, board), not
    just (batch_id, subject_id) — a Batch can have the same subject offered
    under multiple Boards simultaneously, so board is part of the payload
    and part of what identifies "the same offering" on a repeat call.
    """
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    valid_subject_ids = {
        row.id for row in db.query(Subject.id).filter(
            Subject.id.in_(payload.subject_ids), Subject.deleted_at.is_(None),
        ).all()
    }
    invalid_subject_ids = set(payload.subject_ids) - valid_subject_ids
    if invalid_subject_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Subject(s) not found: {sorted(str(i) for i in invalid_subject_ids)}",
        )

    board_value = payload.board.value
    existing_by_subject = {
        row.subject_id: row for row in db.query(BatchSubject).filter(
            BatchSubject.batch_id == batch_id,
            BatchSubject.subject_id.in_(payload.subject_ids),
            BatchSubject.board == board_value,
        ).all()
    }
    for subject_id in payload.subject_ids:
        existing = existing_by_subject.get(subject_id)
        if existing:
            existing.is_active = payload.is_active
        else:
            db.add(BatchSubject(
                batch_id=batch_id, subject_id=subject_id,
                board=board_value, is_active=payload.is_active,
            ))

    log_action(
        db, current_user.id, "batch_subjects_offered" if payload.is_active else "batch_subjects_withdrawn",
        "batch_subjects", batch_id, None,
        {"subject_ids": [str(i) for i in payload.subject_ids], "board": board_value, "is_active": payload.is_active},
    )
    db.commit()

    # Return the batch's full current offered list (not just what this call
    # touched) — matches what GET returns, so the frontend can replace its
    # whole "currently offered" panel from this one response. Same
    # SubjectLevel join as list_offered_subjects above, for the same
    # multi-level-subject reason — kept in sync deliberately since this
    # duplicates that query rather than calling it (this handler already
    # has its own db.commit() boundary to return fresh post-write state).
    rows = (
        db.query(BatchSubject, Subject.name.label("subject_name"),
                  SubjectLevel.level_id.label("level_id"), Level.name.label("level_name"))
        .join(Subject, Subject.id == BatchSubject.subject_id)
        .join(SubjectLevel, SubjectLevel.subject_id == Subject.id)
        .join(Level, Level.id == SubjectLevel.level_id)
        .filter(BatchSubject.batch_id == batch_id, BatchSubject.is_active.is_(True))
        .order_by(Level.display_order, Subject.name)
        .all()
    )
    return [
        BatchSubjectOut(subject_id=bs.subject_id, subject_name=subject_name,
                         level_id=level_id, level_name=level_name,
                         board=bs.board, is_active=bs.is_active)
        for bs, subject_name, level_id, level_name in rows
    ]


@router.post("/batches/{batch_id}/assign-teacher", response_model=TeacherSubjectAssignmentOut,
             status_code=status.HTTP_201_CREATED)
def assign_teacher_to_batch(batch_id: uuid.UUID, payload: AssignTeacherToBatchPayload,
                             db: Session = Depends(get_db),
                             current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Batch-scoped convenience wrapper around the exact same
    TeacherSubjectAssignment row that POST /api/academic/teacher-assignments
    already creates — matches the Admin Batches screen's cascading-dropdown
    flow (spec section 4), which is already "inside" a batch by the time it
    gets to picking a teacher, so batch_id belongs in the URL, not the body.
    Because it's the same table, an assignment made here shows up
    immediately in GET /teacher-assignments/registry (Information Registry)
    and vice versa — there's no separate sync step, they were never two
    different tables to begin with.
    """
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    subject = db.query(Subject).filter(Subject.id == payload.subject_id, Subject.deleted_at.is_(None)).first()
    if not subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

    # Admin Role Isolation Guard: explicit, unfiltered lookup + guard first
    # so an admin/superadmin id gets a clear 400 ("cannot be assigned as a
    # Teacher") instead of a generic 404 that happens to fall out of the
    # role-scoped eligibility query below. See app/core/security.py.
    target_user = db.query(User).filter(User.id == payload.teacher_id, User.deleted_at.is_(None)).first()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")
    guard_teacher_assignee_role(target_user.role, context="a Teacher on a Subject/Batch assignment")

    # Same dual-role allowance as GET /api/users?role=teacher (see that
    # endpoint's comment) — a Coordinator who kept their teacher_profiles
    # row from before being promoted is a legitimate Teacher assignee too,
    # not just users whose current User.role is literally 'teacher'.
    teacher = db.query(User).filter(
        User.id == payload.teacher_id,
        or_(
            User.role == "teacher",
            and_(
                User.role == "coordinator",
                User.id.in_(db.query(TeacherProfile.user_id)),
            ),
        ),
        User.deleted_at.is_(None),
    ).first()
    if not teacher:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")

    existing = db.query(TeacherSubjectAssignment).filter(
        TeacherSubjectAssignment.teacher_id == payload.teacher_id,
        TeacherSubjectAssignment.subject_id == payload.subject_id,
        TeacherSubjectAssignment.batch_id == batch_id,
        TeacherSubjectAssignment.deleted_at.is_(None),
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Assignment already exists")

    # Same guard as the generic POST /teacher-assignments above — this is
    # the "batch-scoped convenience wrapper" this endpoint's own docstring
    # describes, so it needs the exact same check: no assigning a teacher
    # into a batch/subject combo with no active offering.
    boards = active_boards_for(db, batch_id, payload.subject_id)
    if not boards:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This subject has no active offering for this batch. Offer the subject for this "
                   "batch (and board) before assigning a teacher to it.",
        )

    assignment = TeacherSubjectAssignment(
        assigned_by=current_user.id, teacher_id=payload.teacher_id,
        subject_id=payload.subject_id, batch_id=batch_id,
    )
    db.add(assignment)
    db.flush()  # populate assignment.id (server_default) before logging it
    log_action(db, current_user.id, "teacher_assigned_to_batch", "teacher_subject_assignments", assignment.id,
               None, {"batch_id": str(batch_id), "subject_id": str(payload.subject_id), "teacher_id": str(payload.teacher_id)})
    db.commit()
    db.refresh(assignment)
    return TeacherSubjectAssignmentOut(
        id=assignment.id, teacher_id=assignment.teacher_id, subject_id=assignment.subject_id,
        batch_id=assignment.batch_id, assigned_by=assignment.assigned_by, assigned_at=assignment.assigned_at,
        board=sorted(boards)[0],
    )


# ---------------------------------------------------------------------------
# Levels & Subjects.
#
# schema_update_16: reverses schema_update_11's "read-only, seed-only"
# stance for Subjects, per updated workflow — POST /subjects is back
# (Admin/Coordinator only), with case-insensitive name/code duplicate
# checking and multi-level mapping via subject_levels. Levels remain
# read-only through this API (still seed/migration-managed — see
# schema_update_16.sql) but GET /levels is now scoped to exactly the 4
# standardized, active levels instead of every non-deleted row.
# ---------------------------------------------------------------------------
@router.get("/levels", response_model=List[LevelOut])
def list_levels(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return (
        db.query(Level)
        .filter(Level.deleted_at.is_(None), Level.is_active.is_(True), Level.code.in_(STANDARD_LEVEL_CODES))
        .order_by(Level.display_order)
        .all()
    )


@router.get("/subjects", response_model=List[SubjectOut])
def list_subjects(level_id: Optional[uuid.UUID] = None, include_inactive: bool = False,
                   db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Subject catalog, optionally filtered to one Level. Powers every
    subject dropdown in the app (Subject Requests, Teacher Assignment,
    Enrollment, Offer Subjects).

    include_inactive: every dropdown consumer wants active-only (the
    default, unchanged). The Admin Subjects management screen is the one
    exception — it passes include_inactive=true because its whole job is
    activating/deactivating subjects, so it has to be able to see and
    re-activate one that's already off. Without this, a deactivated
    subject vanished from that screen too on the next reload, with no way
    back in (see set_subject_status in routers/subjects.py — deactivate
    is meant to be reversible, but wasn't reachable once hidden here).

    BUG FIX: previously filtered ONLY on the primary Subject.level_id FK,
    ignoring subject_levels entirely. A multi-level subject (e.g. Physics
    checked for both O-Level AND AS-Level in the Add Subject dialog) has
    level_id set to whichever level was picked first — schema_update_16b's
    own comment on subject_levels says every additionally-selected level
    "is additionally written to subject_levels", clearly intending those
    subjects to be findable under any of their levels. Filtering only on
    the primary FK meant such a subject was invisible in "Offer Subjects"
    (and therefore un-offer-able, and therefore un-assignable to a
    Teacher) for every level except the one that happened to be picked
    first — this is why "add a teacher to a subject" could silently fail:
    the subject the Coordinator wanted was never in the catalog dropdown
    to begin with under the level they had selected. Now matches either
    the primary FK or a subject_levels row for the requested level.
    """
    query = (
        db.query(Subject, Level.name.label("level_name"))
        .join(Level, Level.id == Subject.level_id)
        .filter(Subject.deleted_at.is_(None))
    )
    if not include_inactive:
        query = query.filter(Subject.is_active.is_(True))
    if level_id:
        query = query.filter(
            or_(
                Subject.level_id == level_id,
                Subject.id.in_(
                    db.query(SubjectLevel.subject_id).filter(SubjectLevel.level_id == level_id)
                ),
            )
        )
    rows = query.order_by(Level.display_order, Subject.name).all()

    subject_ids = [subject.id for subject, _ in rows]
    levels_by_subject: dict[uuid.UUID, list[Level]] = {sid: [] for sid in subject_ids}
    if subject_ids:
        mapped = (
            db.query(SubjectLevel.subject_id, Level)
            .join(Level, Level.id == SubjectLevel.level_id)
            .filter(SubjectLevel.subject_id.in_(subject_ids))
            .order_by(Level.display_order)
            .all()
        )
        for subject_id, level in mapped:
            levels_by_subject[subject_id].append(level)

    return [
        SubjectOut(
            id=subject.id, name=subject.name, code=subject.code, board=subject.board,
            is_active=subject.is_active, level_id=subject.level_id, level_name=level_name,
            levels=levels_by_subject.get(subject.id, []),
        )
        for subject, level_name in rows
    ]


@router.post("/subjects", response_model=SubjectOut, status_code=status.HTTP_201_CREATED)
def create_subject(payload: SubjectCreate, db: Session = Depends(get_db),
                    current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Admin/Coordinator: add a new catalog Subject and map it to one or
    more of the 4 standardized Levels (schema_update_16). Rejects a
    duplicate name OR code, case-insensitively, before writing anything.
    """
    valid_levels = (
        db.query(Level)
        .filter(
            Level.id.in_(payload.level_ids), Level.deleted_at.is_(None),
            Level.is_active.is_(True), Level.code.in_(STANDARD_LEVEL_CODES),
        )
        .all()
    )
    valid_level_ids = {level.id for level in valid_levels}
    invalid_level_ids = set(payload.level_ids) - valid_level_ids
    if invalid_level_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Level(s) not found or inactive: {sorted(str(i) for i in invalid_level_ids)}",
        )

    duplicate = (
        db.query(Subject)
        .filter(
            Subject.deleted_at.is_(None),
            or_(
                func.lower(Subject.name) == payload.name.lower(),
                func.lower(Subject.code) == payload.code.lower(),
            ),
        )
        .first()
    )
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Subject with this name or code already exists in the catalog.",
        )

    # level_id stays populated (first selected level) purely so existing
    # single-level consumers (offer-subjects, teacher assignment,
    # enrollment.level_id) keep working — see Subject's model docstring.
    ordered_level_ids = [lid for lid in payload.level_ids if lid in valid_level_ids]
    primary_level_id = ordered_level_ids[0]

    subject = Subject(
        name=payload.name, code=payload.code, board=payload.board.value,
        level_id=primary_level_id, is_active=True,
    )
    db.add(subject)
    db.flush()  # populate subject.id before writing subject_levels

    for level_id in ordered_level_ids:
        db.add(SubjectLevel(subject_id=subject.id, level_id=level_id))

    log_action(
        db, current_user.id, "subject_created", "subjects", subject.id, None,
        {"name": payload.name, "code": payload.code, "board": payload.board.value,
         "level_ids": [str(i) for i in ordered_level_ids]},
    )
    db.commit()
    db.refresh(subject)

    levels_out = [level for level in valid_levels if level.id in set(ordered_level_ids)]
    levels_out.sort(key=lambda lvl: lvl.display_order)
    level_name = next((level.name for level in valid_levels if level.id == primary_level_id), None)
    return SubjectOut(
        id=subject.id, name=subject.name, code=subject.code, board=subject.board,
        is_active=subject.is_active, level_id=subject.level_id, level_name=level_name,
        levels=levels_out,
    )


# ---------------------------------------------------------------------------
# Student level enrollments (O Level / AS Level / A2 Level)
# ---------------------------------------------------------------------------
@router.post("/level-enrollments", response_model=StudentLevelEnrollmentOut, status_code=status.HTTP_201_CREATED)
def create_level_enrollment(payload: StudentLevelEnrollmentCreate, db: Session = Depends(get_db),
                             current_user: User = Depends(require_roles("admin", "coordinator"))):
    # Business rule (procedural, not a DB constraint, to allow transfer/edge cases):
    # O Level must be 'completed' before an AS Level row can be created.
    level = db.query(Level).filter(Level.id == payload.level_id, Level.deleted_at.is_(None)).first()
    if not level:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Level not found")

    if level.name.lower().startswith("as"):
        o_level = db.query(Level).filter(Level.name.ilike("o level")).first()
        if o_level:
            completed_o_level = db.query(StudentLevelEnrollment).filter(
                StudentLevelEnrollment.student_id == payload.student_id,
                StudentLevelEnrollment.level_id == o_level.id,
                StudentLevelEnrollment.status == "completed",
                StudentLevelEnrollment.deleted_at.is_(None),
            ).first()
            if not completed_o_level:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Student must have a completed O Level enrollment before enrolling in AS Level",
                )

    enrollment = StudentLevelEnrollment(**payload.model_dump())
    db.add(enrollment)
    db.commit()
    db.refresh(enrollment)
    return enrollment


@router.get("/level-enrollments", response_model=List[StudentLevelEnrollmentOut])
def list_level_enrollments(student_id: Optional[uuid.UUID] = None, db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    query = db.query(StudentLevelEnrollment).filter(StudentLevelEnrollment.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(StudentLevelEnrollment.student_id == current_user.id)
    elif student_id:
        query = query.filter(StudentLevelEnrollment.student_id == student_id)
    return query.all()


# ---------------------------------------------------------------------------
# Subject requests -> approval auto-creates an enrollment row
# ---------------------------------------------------------------------------
@router.post("/subject-requests", response_model=SubjectRequestOut, status_code=status.HTTP_201_CREATED)
def create_subject_request(payload: SubjectRequestCreate, db: Session = Depends(get_db),
                            current_user: User = Depends(require_roles("student"))):
    req = SubjectRequest(student_id=current_user.id, **payload.model_dump())
    db.add(req)
    db.flush()  # assigns req.id before we reference it in notifications below

    # Notify everyone who can actually approve/reject this (Coordinators and
    # Admins both can, per the permission matrix) — no routing split, same
    # pattern as complaints being visible to both roles at once.
    subject = db.query(Subject).filter(Subject.id == req.subject_id).first()
    subject_name = subject.name if subject else "a subject"
    reviewers = db.query(User).filter(
        User.role.in_(("admin", "coordinator")), User.deleted_at.is_(None), User.status == "active",
    ).all()
    for reviewer in reviewers:
        notify(
            db, reviewer.id, "subject_request_submitted",
            f"{current_user.full_name} requested {subject_name}.",
            related_entity_type="subject_requests", related_entity_id=req.id,
        )

    db.commit()
    db.refresh(req)
    return req


@router.get("/subject-requests", response_model=List[SubjectRequestOut])
def list_subject_requests(status_filter: Optional[str] = None, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    query = db.query(SubjectRequest).filter(SubjectRequest.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(SubjectRequest.student_id == current_user.id)
    elif current_user.role not in ("admin", "coordinator"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
    if status_filter:
        query = query.filter(SubjectRequest.status == status_filter)
    return query.order_by(SubjectRequest.requested_at.desc()).all()


@router.get("/subject-requests/review-queue", response_model=List[SubjectRequestReviewRowOut])
def list_subject_request_review_queue(
    status_filter: Optional[str] = "requested",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Coordinator/Admin queue — defaults to just the pending ones (status_filter
    defaults to "requested", which is what's displayed as "Pending" in the UI;
    the enum itself is unchanged to avoid a migration). Pass status_filter=""
    or any specific value to see approved/rejected history too.
    """
    query = db.query(
        SubjectRequest, User.full_name.label("student_name"),
        Subject.name.label("subject_name"), Batch.name.label("batch_name"),
    ).join(
        User, User.id == SubjectRequest.student_id
    ).join(
        Subject, Subject.id == SubjectRequest.subject_id
    ).join(
        Batch, Batch.id == SubjectRequest.batch_id
    ).filter(SubjectRequest.deleted_at.is_(None))

    if status_filter:
        query = query.filter(SubjectRequest.status == status_filter)

    rows = query.order_by(SubjectRequest.requested_at.desc()).all()
    return [
        SubjectRequestReviewRowOut(
            id=req.id, student_id=req.student_id, student_name=student_name,
            subject_id=req.subject_id, subject_name=subject_name,
            batch_id=req.batch_id, batch_name=batch_name, status=req.status,
            requested_at=req.requested_at, actioned_by=req.actioned_by, actioned_at=req.actioned_at,
        )
        for req, student_name, subject_name, batch_name in rows
    ]


@router.patch("/subject-requests/{request_id}", response_model=SubjectRequestOut)
def review_subject_request(request_id: uuid.UUID, payload: SubjectRequestReview, db: Session = Depends(get_db),
                            current_user: User = Depends(require_roles("admin", "coordinator"))):
    req = db.query(SubjectRequest).filter(SubjectRequest.id == request_id, SubjectRequest.deleted_at.is_(None)).first()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject request not found")
    if req.status != "requested":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This request has already been actioned")
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be approved or rejected")

    req.status = payload.status
    req.actioned_by = current_user.id
    req.actioned_at = datetime.now(timezone.utc)

    if payload.status == "approved":
        existing = db.query(Enrollment).filter(
            Enrollment.student_id == req.student_id, Enrollment.subject_id == req.subject_id,
            Enrollment.batch_id == req.batch_id,
        ).first()
        if not existing:
            db.add(Enrollment(student_id=req.student_id, subject_id=req.subject_id, batch_id=req.batch_id))

    subject = db.query(Subject).filter(Subject.id == req.subject_id).first()
    subject_name = subject.name if subject else "the subject"
    verb = "approved" if payload.status == "approved" else "rejected"
    message = f"Your request for {subject_name} was {verb}."
    if payload.comment:
        message += f" Comment: {payload.comment}"
    notify(
        db, req.student_id, "subject_request_reviewed", message,
        related_entity_type="subject_requests", related_entity_id=req.id,
    )

    log_action(db, current_user.id, "subject_request_reviewed", "subject_requests", req.id, None,
               {"status": payload.status, "comment": payload.comment})
    db.commit()
    db.refresh(req)
    return req


@router.get("/enrollments", response_model=List[EnrollmentOut])
def list_enrollments(student_id: Optional[uuid.UUID] = None, subject_id: Optional[uuid.UUID] = None,
                      db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = db.query(Enrollment).filter(Enrollment.deleted_at.is_(None))
    if current_user.role == "student":
        query = query.filter(Enrollment.student_id == current_user.id)
    else:
        if student_id:
            query = query.filter(Enrollment.student_id == student_id)
    if subject_id:
        query = query.filter(Enrollment.subject_id == subject_id)
    return query.all()


# ---------------------------------------------------------------------------
# Teacher <-> Subject assignments (Coordinator by default, Admin also allowed)
# ---------------------------------------------------------------------------
@router.post("/teacher-assignments", response_model=TeacherSubjectAssignmentOut, status_code=status.HTTP_201_CREATED)
def assign_teacher(payload: TeacherSubjectAssignmentCreate, db: Session = Depends(get_db),
                    current_user: User = Depends(require_roles("admin", "coordinator"))):
    # Admin Role Isolation Guard: fetch the target user and reject up front
    # if they're an admin-tier account — this endpoint previously built the
    # TeacherSubjectAssignment straight off payload.teacher_id with no role
    # check at all, so any admin/superadmin user_id could be assigned as a
    # Teacher. See app/core/security.py::guard_teacher_assignee_role.
    target_user = db.query(User).filter(User.id == payload.teacher_id, User.deleted_at.is_(None)).first()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")
    guard_teacher_assignee_role(target_user.role, context="a Teacher on a Subject/Batch assignment")

    existing = db.query(TeacherSubjectAssignment).filter(
        TeacherSubjectAssignment.teacher_id == payload.teacher_id,
        TeacherSubjectAssignment.subject_id == payload.subject_id,
        TeacherSubjectAssignment.batch_id == payload.batch_id,
        TeacherSubjectAssignment.deleted_at.is_(None),
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Assignment already exists")

    # A Teacher must never be assignable to a subject+batch combo that
    # isn't an actual active offering (BatchSubject) — otherwise this
    # assignment shows up verbatim in every Teacher-scoped cascading
    # dropdown (Marks entry's Batch -> Board -> Level -> Subject picker)
    # for a batch/subject the Admin never really offered, or withdrew.
    boards = active_boards_for(db, payload.batch_id, payload.subject_id)
    if not boards:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This subject has no active offering for this batch. Offer the subject for this "
                   "batch (and board) before assigning a teacher to it.",
        )

    assignment = TeacherSubjectAssignment(assigned_by=current_user.id, **payload.model_dump())
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return TeacherSubjectAssignmentOut(
        id=assignment.id, teacher_id=assignment.teacher_id, subject_id=assignment.subject_id,
        batch_id=assignment.batch_id, assigned_by=assignment.assigned_by, assigned_at=assignment.assigned_at,
        board=sorted(boards)[0],
    )


@router.get("/teacher-assignments", response_model=List[TeacherSubjectAssignmentOut])
def list_teacher_assignments(
    teacher_id: Optional[uuid.UUID] = None,
    subject_id: Optional[uuid.UUID] = None,
    batch_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """
    subject_id/batch_id filters added for the cascading-dropdown pattern
    (Subject -> eligible Teachers -> eligible Batches, for any future
    "assign a teacher to a subject" screen) — no current UI calls these
    yet, added so the endpoint is ready rather than needing another round
    trip later. teacher_id filtering (existing) still works the same.

    Over-Inclusive Cascading Dropdowns fix: a raw (non-deleted)
    TeacherSubjectAssignment row is no longer enough on its own — the
    batch could since have been archived/soft-deleted, or the subject's
    offering for that batch withdrawn (BatchSubject.is_active=False) or
    never created at all (pre-existing data / assignments made before this
    fix's validation went in on the write side). Every row returned here
    is now cross-checked against an ACTUAL active offering, and fanned out
    one row per active board that offering is under — this is what lets
    the Teacher Portal's Batch -> Board -> Level -> Subject cascade (Marks
    entry) show only boards genuinely active for that batch/subject
    combo, instead of inferring board from the catalog Subject's own
    `board` (which can be "All", and previously made every board show up
    regardless of what's actually offered here).
    """
    query = (
        db.query(TeacherSubjectAssignment)
        .join(Batch, Batch.id == TeacherSubjectAssignment.batch_id)
        .join(Subject, Subject.id == TeacherSubjectAssignment.subject_id)
        .filter(
            TeacherSubjectAssignment.deleted_at.is_(None),
            Batch.deleted_at.is_(None),
            Subject.deleted_at.is_(None),
        )
    )
    if current_user.role == "teacher":
        query = query.filter(TeacherSubjectAssignment.teacher_id == current_user.id)
    elif teacher_id:
        query = query.filter(TeacherSubjectAssignment.teacher_id == teacher_id)
    if subject_id:
        query = query.filter(TeacherSubjectAssignment.subject_id == subject_id)
    if batch_id:
        query = query.filter(TeacherSubjectAssignment.batch_id == batch_id)
    assignments = query.all()

    boards_by_pair = active_boards_map(db, ((a.batch_id, a.subject_id) for a in assignments))

    result: List[TeacherSubjectAssignmentOut] = []
    for a in assignments:
        for board in boards_by_pair.get((a.batch_id, a.subject_id), []):
            result.append(TeacherSubjectAssignmentOut(
                id=a.id, teacher_id=a.teacher_id, subject_id=a.subject_id, batch_id=a.batch_id,
                assigned_by=a.assigned_by, assigned_at=a.assigned_at, board=board,
            ))
    return result


@router.get("/teacher-assignments/registry", response_model=List[TeacherAssignmentRegistryOut])
def teacher_assignments_for_registry(teacher_id: uuid.UUID, db: Session = Depends(get_db),
                                      current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Information Registry (spec module 2): "Teacher info stored... Classes
    taught, subjects taught." Joined display variant of the endpoint above —
    Admin/Coordinator only, since this is a registry-viewing feature, not
    something a Teacher needs about themselves via this route (they'd use
    the un-joined /teacher-assignments with no teacher_id, same as before).
    """
    rows = (
        db.query(TeacherSubjectAssignment, Subject.name.label("subject_name"), Batch.name.label("batch_name"))
        .join(Subject, Subject.id == TeacherSubjectAssignment.subject_id)
        .join(Batch, Batch.id == TeacherSubjectAssignment.batch_id)
        .filter(
            TeacherSubjectAssignment.teacher_id == teacher_id,
            TeacherSubjectAssignment.deleted_at.is_(None),
        )
        .order_by(Batch.year.desc(), Subject.name)
        .all()
    )
    return [
        TeacherAssignmentRegistryOut(
            subject_id=assignment.subject_id, subject_name=subject_name,
            batch_id=assignment.batch_id, batch_name=batch_name,
        )
        for assignment, subject_name, batch_name in rows
    ]


@router.get("/student-enrollments/registry", response_model=StudentEnrollmentRegistryOut)
def student_enrollments_for_registry(student_id: uuid.UUID, db: Session = Depends(get_db),
                                      current_user: User = Depends(require_roles("admin", "coordinator"))):
    """
    Information Registry (spec module 2): "Student info stored... level,
    registered courses." Student-side counterpart to
    teacher_assignments_for_registry above — Admin/Coordinator only, joined
    display variant used by the Registry's Student Edit Details view (both
    for the read-only summary and to prefill the level/subjects editors,
    see PATCH /api/users/{user_id}).
    """
    active_level_enrollment = (
        db.query(StudentLevelEnrollment)
        .filter(
            StudentLevelEnrollment.student_id == student_id,
            StudentLevelEnrollment.status == "active",
            StudentLevelEnrollment.deleted_at.is_(None),
        )
        .order_by(StudentLevelEnrollment.started_at.desc())
        .first()
    )
    current_level_id = None
    current_level_name = None
    if active_level_enrollment:
        level = db.query(Level).filter(Level.id == active_level_enrollment.level_id).first()
        current_level_id = active_level_enrollment.level_id
        current_level_name = level.name if level else None

    rows = (
        db.query(Enrollment, Subject.name.label("subject_name"), Batch.name.label("batch_name"),
                  Level.name.label("level_name"))
        .join(Subject, Subject.id == Enrollment.subject_id)
        .join(Batch, Batch.id == Enrollment.batch_id)
        .outerjoin(Level, Level.id == Enrollment.level_id)
        .filter(
            Enrollment.student_id == student_id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        )
        .order_by(Subject.name)
        .all()
    )
    subjects = [
        StudentEnrollmentRegistrySubject(
            subject_id=enrollment.subject_id, subject_name=subject_name,
            batch_id=enrollment.batch_id, batch_name=batch_name, status=enrollment.status,
            level_id=enrollment.level_id, level_name=level_name,
        )
        for enrollment, subject_name, batch_name, level_name in rows
    ]

    return StudentEnrollmentRegistryOut(
        current_level_id=current_level_id,
        current_level_name=current_level_name,
        subjects=subjects,
    )


# ---------------------------------------------------------------------------
# Student "me" endpoints — the Angular dashboard/timetable screens call these
# directly (see AcademicService.getMyTimetable / getDashboardSummary).
# ---------------------------------------------------------------------------
@router.get("/timetable/me", response_model=List[TimetableEntryOut])
def my_timetable_detailed(db: Session = Depends(get_db),
                           current_user: User = Depends(require_roles("student"))):
    """
    Same derived-view join as /api/timetable/my-timetable, but enriched with
    subject_name and teacher_name so the frontend grid doesn't need extra
    lookups per row.
    """
    subject_ids = [
        row.subject_id for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        ).all()
    ]
    if not subject_ids:
        return []

    rows = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), User.full_name.label("teacher_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(User, User.id == TimetableSlot.teacher_id)
        .filter(TimetableSlot.subject_id.in_(subject_ids), TimetableSlot.deleted_at.is_(None))
        .order_by(TimetableSlot.day_of_week, TimetableSlot.start_time)
        .all()
    )
    return [
        TimetableEntryOut(
            id=slot.id,
            subject_id=slot.subject_id,
            subject_name=subject_name,
            teacher_name=teacher_name,
            day_of_week=slot.day_of_week,
            start_time=slot.start_time,
            end_time=slot.end_time,
        )
        for slot, subject_name, teacher_name in rows
    ]


@router.get("/dashboard/summary", response_model=DashboardSummaryOut)
def dashboard_summary(db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("student"))):
    """
    High-level metadata for the student dashboard's top cards:
    overall attendance %, pending (unmarked, published) assessments,
    and current batch context.

    Note: FUSE LMS has no separate "assignment submission" concept in the
    schema — "pending assignments" is interpreted as published assessments
    in the student's active subjects that don't have a marks row yet.
    """
    current_batch = db.query(Batch).filter(Batch.is_current.is_(True), Batch.deleted_at.is_(None)).first()

    active_subject_ids = [
        row.subject_id for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        ).all()
    ]

    total_periods = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == current_user.id, AttendanceRecord.deleted_at.is_(None)
    ).count()
    attended_periods = db.query(AttendanceRecord).filter(
        AttendanceRecord.user_id == current_user.id,
        AttendanceRecord.deleted_at.is_(None),
        AttendanceRecord.status.in_(["present", "late"]),
    ).count()
    attendance_percentage = round((attended_periods / total_periods) * 100, 1) if total_periods else 0.0

    pending_count = 0
    if active_subject_ids:
        published = db.query(Assessment).filter(
            Assessment.subject_id.in_(active_subject_ids),
            Assessment.status == "published",
            Assessment.deleted_at.is_(None),
        ).all()
        for assessment in published:
            has_mark = db.query(Mark).filter(
                Mark.assessment_id == assessment.id,
                Mark.student_id == current_user.id,
                Mark.deleted_at.is_(None),
            ).first()
            if not has_mark:
                pending_count += 1

    return DashboardSummaryOut(
        attendance_percentage=attendance_percentage,
        pending_assessments_count=pending_count,
        current_batch_name=current_batch.name if current_batch else None,
        current_batch_year=current_batch.year if current_batch else None,
        active_subjects_count=len(active_subject_ids),
    )