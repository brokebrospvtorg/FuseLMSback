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
from typing import Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.models import (
    User, TeacherProfile, TeacherBoard, TeacherLevel, Level,
    TeacherSubjectAssignment, Subject, Batch,
)
from app.schemas.teacher import (
    TeacherWorkloadSummaryOut, TeacherWorkloadLevelOut, TeacherWorkloadAssignmentOut,
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
