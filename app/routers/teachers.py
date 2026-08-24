"""
Teachers workload module — powers the Admin/Coordinator Portal's "Teachers"
sidebar section list view.

Mounted separately from app/routers/users.py (which owns POST/GET/PUT
/api/users, including the flat ?role=teacher dropdown listing) because this
endpoint returns a materially heavier/different shape — per-teacher
workload (boards, levels, active subject+batch assignments) — purpose-built
for one screen rather than a general user list.
"""
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.core.audit import log_action
from app.core.offering_utils import active_boards_for
from app.core.security import guard_teacher_assignee_role
from app.models import (
    User, TeacherProfile, TeacherBoard, TeacherLevel, Level,
    TeacherSubjectAssignment, Subject, Batch,
)
from app.schemas.academic import TeacherSubjectAssignmentOut
from app.schemas.teacher import (
    TeacherWorkloadSummaryOut, TeacherWorkloadLevelOut, TeacherWorkloadAssignmentOut,
    TeacherAssignmentCreateRequest,
)

router = APIRouter(prefix="/api/teachers", tags=["teachers"], dependencies=[Depends(check_license)])


@router.get("/workload-summary", response_model=List[TeacherWorkloadSummaryOut])
def get_teacher_workload_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Aggregated per-Teacher workload for the Teachers sidebar section.

    "Active teachers" = users.deleted_at IS NULL AND users.status ==
    'active' AND holds a teacher_profiles row, where role == 'teacher' OR
    (role == 'coordinator' AND still holds a teacher_profiles row) — same
    dual-role definition list_users(role="teacher") in
    app/routers/users.py already uses, so this sidebar and the existing
    Teacher-picker dropdown never disagree on who counts as a Teacher.

    Assigned Boards / Levels are read straight from teacher_boards /
    teacher_levels — neither table has a deleted_at column, so every row
    there is, by definition, a current qualification.

    "Active assigned subjects and batches" = teacher_subject_assignments
    rows with deleted_at IS NULL, inner-joined to subjects/batches that are
    themselves not soft-deleted (a soft-deleted Subject/Batch shouldn't
    surface on the sidebar even if the assignment row itself is untouched).
    """
    # ---- Base: active, non-deleted teachers (incl. teacher-turned-coordinator) ----
    teacher_rows = (
        db.query(User, TeacherProfile)
        .join(TeacherProfile, TeacherProfile.user_id == User.id)
        .filter(
            User.deleted_at.is_(None),
            User.status == "active",
            or_(User.role == "teacher", User.role == "coordinator"),
        )
        .order_by(User.full_name)
        .all()
    )
    if not teacher_rows:
        return []

    teacher_ids = [user.id for user, _ in teacher_rows]

    # ---- Assigned Boards ----
    board_rows = (
        db.query(TeacherBoard.teacher_id, TeacherBoard.board)
        .filter(TeacherBoard.teacher_id.in_(teacher_ids))
        .all()
    )
    boards_by_teacher: Dict[uuid.UUID, List[str]] = defaultdict(list)
    for teacher_id, board in board_rows:
        boards_by_teacher[teacher_id].append(board)

    # ---- Assigned Levels ----
    level_rows = (
        db.query(TeacherLevel.teacher_id, Level.id, Level.name)
        .join(Level, Level.id == TeacherLevel.level_id)
        .filter(TeacherLevel.teacher_id.in_(teacher_ids))
        .all()
    )
    levels_by_teacher: Dict[uuid.UUID, List[TeacherWorkloadLevelOut]] = defaultdict(list)
    for teacher_id, level_id, level_name in level_rows:
        levels_by_teacher[teacher_id].append(
            TeacherWorkloadLevelOut(level_id=level_id, level_name=level_name)
        )

    # ---- Active assigned subjects & batches ----
    assignment_rows = (
        db.query(
            TeacherSubjectAssignment.teacher_id,
            Subject.id.label("subject_id"),
            Subject.name.label("subject_name"),
            Batch.id.label("batch_id"),
            Batch.name.label("batch_name"),
        )
        .join(Subject, Subject.id == TeacherSubjectAssignment.subject_id)
        .join(Batch, Batch.id == TeacherSubjectAssignment.batch_id)
        .filter(
            TeacherSubjectAssignment.teacher_id.in_(teacher_ids),
            TeacherSubjectAssignment.deleted_at.is_(None),
            Subject.deleted_at.is_(None),
            Batch.deleted_at.is_(None),
        )
        .order_by(Subject.name, Batch.name)
        .all()
    )
    assignments_by_teacher: Dict[uuid.UUID, List[TeacherWorkloadAssignmentOut]] = defaultdict(list)
    for teacher_id, subject_id, subject_name, batch_id, batch_name in assignment_rows:
        assignments_by_teacher[teacher_id].append(
            TeacherWorkloadAssignmentOut(
                subject_id=subject_id, subject_name=subject_name,
                batch_id=batch_id, batch_name=batch_name,
            )
        )

    # ---- Assemble one row per teacher ----
    result: List[TeacherWorkloadSummaryOut] = []
    for user, profile in teacher_rows:
        assignments = assignments_by_teacher.get(user.id, [])
        result.append(TeacherWorkloadSummaryOut(
            id=user.id,
            full_name=user.full_name,
            email=user.email,
            teacher_code=profile.teacher_code,
            phone_number=user.phone_number,
            boards=boards_by_teacher.get(user.id, []),
            levels=levels_by_teacher.get(user.id, []),
            assignments=assignments,
            active_subjects_count=len({a.subject_id for a in assignments}),
            active_batches_count=len({a.batch_id for a in assignments}),
        ))
    return result


# ---------------------------------------------------------------------------
# Workload Management — add/remove one Teacher's batch/subject assignments.
#
# Deliberately teacher-scoped (mounted under /api/teachers/{teacher_id}/...)
# rather than living only on the generic /api/academic/teacher-assignments
# collection — this is the Teachers sidebar's own "manage this teacher's
# workload" action, so teacher_id belongs in the URL, not repeated in every
# request body. Both endpoints write/soft-delete the exact same
# teacher_subject_assignments row the academic.py endpoints do — there's
# only ever one table — so a change made here shows up immediately in GET
# /api/academic/teacher-assignments, the Registry, and workload-summary
# above, with no separate sync step.
# ---------------------------------------------------------------------------
def _resolve_assignable_teacher(db: Session, teacher_id: uuid.UUID) -> User:
    """Shared eligibility check for both endpoints below — same rules as
    assign_teacher_to_batch in app/routers/academic.py:
      1. Must resolve to a real, non-deleted user at all (else 404) —
         checked unfiltered-by-role first so an admin/superadmin id gets a
         clear 400 from the guard below instead of an incidental 404.
      2. Must not be an admin-tier account (guard_teacher_assignee_role).
      3. Must actually be eligible as a Teacher assignee: role == 'teacher',
         OR role == 'coordinator' who still holds a teacher_profiles row
         (dual-role — same definition used everywhere else a teacher_id is
         accepted, so this endpoint never disagrees with GET
         /api/users?role=teacher on who counts as a Teacher).
    """
    target_user = db.query(User).filter(User.id == teacher_id, User.deleted_at.is_(None)).first()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Teacher not found")
    guard_teacher_assignee_role(target_user.role, context="a Teacher on a Subject/Batch assignment")

    teacher = db.query(User).filter(
        User.id == teacher_id,
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
    return teacher


@router.post(
    "/{teacher_id}/assignments",
    response_model=TeacherSubjectAssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
def assign_subject_batch_to_teacher(
    teacher_id: uuid.UUID,
    payload: TeacherAssignmentCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Workload Management — add one (subject, batch) combination to this
    Teacher. Validates, in order:
      1. teacher_id resolves to a real, eligible Teacher assignee.
      2. subject_id / batch_id both resolve to real, non-deleted rows.
      3. The subject actually has an ACTIVE offering for this batch
         (BatchSubject) — same guard as every other assignment-creating
         endpoint (see active_boards_for's docstring for why: without
         this, the assignment would show up in the Teacher's own
         cascading dropdowns for a batch/subject that was never really
         offered, or whose offering was withdrawn).
      4. No duplicate: an ACTIVE (non-deleted) assignment for this exact
         (teacher, subject, batch) triple already means "already
         assigned" -> 409.

    Soft-deletion handling: teacher_subject_assignments carries a DB-level
    unique constraint on (teacher_id, subject_id, batch_id) — NOT scoped to
    only non-deleted rows. So if this exact triple was assigned before and
    later removed (DELETE below, which soft-deletes), a plain INSERT here
    would collide with that still-present-but-deleted row and raise an
    unhandled IntegrityError (500). Instead: if a soft-deleted row for this
    triple exists, REVIVE it (clear deleted_at, refresh assigned_by/
    assigned_at) instead of inserting a duplicate — same pattern already
    used for re-linking a Parent to a Student in
    app/routers/users.py::create_parent_link.
    """
    teacher = _resolve_assignable_teacher(db, teacher_id)

    subject = db.query(Subject).filter(Subject.id == payload.subject_id, Subject.deleted_at.is_(None)).first()
    if not subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

    batch = db.query(Batch).filter(Batch.id == payload.batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    existing = db.query(TeacherSubjectAssignment).filter(
        TeacherSubjectAssignment.teacher_id == teacher_id,
        TeacherSubjectAssignment.subject_id == payload.subject_id,
        TeacherSubjectAssignment.batch_id == payload.batch_id,
    ).first()

    if existing and existing.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This teacher is already assigned to this subject/batch.")

    boards = active_boards_for(db, payload.batch_id, payload.subject_id)
    if not boards:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This subject has no active offering for this batch. Offer the subject for this "
                   "batch (and board) before assigning a teacher to it.",
        )

    if existing:
        # Revive rather than insert — see docstring above.
        existing.deleted_at = None
        existing.assigned_by = current_user.id
        existing.assigned_at = datetime.now(timezone.utc)
        log_action(db, current_user.id, "teacher_assignment_revived", "teacher_subject_assignments", existing.id,
                   None, {"teacher_id": str(teacher_id), "subject_id": str(payload.subject_id), "batch_id": str(payload.batch_id)})
        db.commit()
        db.refresh(existing)
        assignment = existing
    else:
        assignment = TeacherSubjectAssignment(
            assigned_by=current_user.id, teacher_id=teacher_id,
            subject_id=payload.subject_id, batch_id=payload.batch_id,
        )
        db.add(assignment)
        db.flush()  # populate assignment.id (server_default) before logging it
        log_action(db, current_user.id, "teacher_assigned", "teacher_subject_assignments", assignment.id,
                   None, {"teacher_id": str(teacher_id), "subject_id": str(payload.subject_id), "batch_id": str(payload.batch_id)})
        db.commit()
        db.refresh(assignment)

    return TeacherSubjectAssignmentOut(
        id=assignment.id, teacher_id=assignment.teacher_id, subject_id=assignment.subject_id,
        batch_id=assignment.batch_id, assigned_by=assignment.assigned_by, assigned_at=assignment.assigned_at,
        board=sorted(boards)[0],
    )


@router.delete("/{teacher_id}/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def unassign_subject_batch_from_teacher(
    teacher_id: uuid.UUID,
    assignment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Workload Management — remove one (subject, batch) assignment from this
    Teacher. Soft-delete only (deleted_at set), same convention as every
    other assignment/link table in this schema — never a hard DELETE, so
    the row (and its assigned_by/assigned_at history) is still there for
    audit purposes, just excluded from every active-assignment query
    (workload-summary, GET /teacher-assignments, batch drawers, Timetable
    Builder's Teacher Assignee dropdown, etc.).

    assignment_id is scoped to teacher_id in the same filter (not looked up
    by id alone, then checked) — an assignment_id that's real but belongs
    to a DIFFERENT teacher 404s exactly the same as one that doesn't exist
    at all, rather than leaking "this id exists, just not for this
    teacher" as a distinct error.
    """
    assignment = db.query(TeacherSubjectAssignment).filter(
        TeacherSubjectAssignment.id == assignment_id,
        TeacherSubjectAssignment.teacher_id == teacher_id,
        TeacherSubjectAssignment.deleted_at.is_(None),
    ).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    assignment.deleted_at = datetime.now(timezone.utc)
    log_action(db, current_user.id, "teacher_unassigned", "teacher_subject_assignments", assignment.id,
               {"teacher_id": str(teacher_id), "subject_id": str(assignment.subject_id), "batch_id": str(assignment.batch_id)}, None)
    db.commit()
    return None
