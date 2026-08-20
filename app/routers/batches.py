import uuid
from collections import defaultdict
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.models import (
    Batch, Subject, Level, Enrollment, TeacherSubjectAssignment, User,
)
from app.schemas.academic import (
    BatchSummaryOut, BatchSummarySubjectOut, BatchSummaryTeacherOut,
)

# Mounted at /api/v1/batches (not /api/academic) to match this feature's
# spec exactly and to line up with app/routers/subjects.py's now-retired
# /api/v1/subjects convention — if the rest of the API is ever versioned,
# this is the router to match against. GET /api/academic/batches (list) and
# POST /api/academic/batches (create) stay exactly where they are in
# app/routers/academic.py; this router only adds the summary view.
router = APIRouter(prefix="/api/v1/batches", tags=["batches"], dependencies=[Depends(check_license)])


@router.get("/{batch_id}/summary", response_model=BatchSummaryOut)
def get_batch_summary(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Aggregated summary for the clickable Batch card/row's inline detail
    view (drawer/modal on the frontend). Strictly Admin/Coordinator, same
    RBAC as every other administrative academic endpoint.

    Returns:
      - total_active_students: distinct students with an ACTIVE enrollment
        in this batch (any subject).
      - total_assigned_teachers / teachers: distinct teachers with a
        (non-deleted) TeacherSubjectAssignment in this batch, each with the
        subjects they teach here.
      - active_subjects: subjects that have at least one ACTIVE enrollment
        or at least one teacher assignment in this batch. A subject with
        neither is considered inactive for this batch and is left out
        entirely, per spec ("Hide inactive subjects") — this is computed
        dynamically from Enrollment/TeacherSubjectAssignment rather than a
        stored is_active flag, so it's always in sync with real activity.
    """
    batch = db.query(Batch).filter(Batch.id == batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    # ---- Active students (distinct, across all subjects in this batch) ----
    active_student_ids = {
        row.student_id
        for row in db.query(Enrollment.student_id).filter(
            Enrollment.batch_id == batch_id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        ).distinct()
    }

    # ---- Per-subject active enrollment counts ----
    enrollment_rows = (
        db.query(Enrollment.subject_id, Enrollment.student_id)
        .filter(
            Enrollment.batch_id == batch_id,
            Enrollment.status == "active",
            Enrollment.deleted_at.is_(None),
        )
        .all()
    )
    active_students_by_subject: Dict[uuid.UUID, set] = defaultdict(set)
    for subject_id, student_id in enrollment_rows:
        active_students_by_subject[subject_id].add(student_id)

    # ---- Teacher assignments in this batch (subject_id, teacher_id, teacher_name) ----
    assignment_rows = (
        db.query(
            TeacherSubjectAssignment.subject_id,
            TeacherSubjectAssignment.teacher_id,
            User.full_name.label("teacher_name"),
        )
        .join(User, User.id == TeacherSubjectAssignment.teacher_id)
        .filter(
            TeacherSubjectAssignment.batch_id == batch_id,
            TeacherSubjectAssignment.deleted_at.is_(None),
        )
        .all()
    )

    teachers_by_subject: Dict[uuid.UUID, List[str]] = defaultdict(list)
    subjects_by_teacher: Dict[uuid.UUID, List[str]] = defaultdict(list)
    teacher_names: Dict[uuid.UUID, str] = {}
    for subject_id, teacher_id, teacher_name in assignment_rows:
        teachers_by_subject[subject_id].append(teacher_name)
        teacher_names[teacher_id] = teacher_name

    # ---- Subjects touched by this batch (union of enrolled-in / taught-in) ----
    touched_subject_ids = set(active_students_by_subject.keys()) | set(teachers_by_subject.keys())

    active_subjects: List[BatchSummarySubjectOut] = []
    if touched_subject_ids:
        subject_rows = (
            db.query(Subject, Level.name.label("level_name"))
            .join(Level, Level.id == Subject.level_id)
            .filter(Subject.id.in_(touched_subject_ids), Subject.deleted_at.is_(None))
            .order_by(Level.display_order, Subject.name)
            .all()
        )
        for subject, level_name in subject_rows:
            active_subjects.append(BatchSummarySubjectOut(
                subject_id=subject.id,
                subject_name=subject.name,
                level_name=level_name,
                teacher_names=sorted(teachers_by_subject.get(subject.id, [])),
                active_student_count=len(active_students_by_subject.get(subject.id, set())),
            ))

    # ---- Need subject names per teacher for the teacher list on the drawer header ----
    subject_names_by_id = {s.subject_id: s.subject_name for s in active_subjects}
    for subject_id, teacher_id, _ in assignment_rows:
        subject_name = subject_names_by_id.get(subject_id)
        if subject_name:
            subjects_by_teacher[teacher_id].append(subject_name)

    teachers_out = [
        BatchSummaryTeacherOut(
            teacher_id=teacher_id,
            teacher_name=name,
            subjects=sorted(set(subjects_by_teacher.get(teacher_id, []))),
        )
        for teacher_id, name in sorted(teacher_names.items(), key=lambda kv: kv[1])
    ]

    return BatchSummaryOut(
        batch_id=batch.id,
        batch_name=batch.name,
        board=batch.board,
        is_current=batch.is_current,
        total_active_students=len(active_student_ids),
        total_assigned_teachers=len(teacher_names),
        teachers=teachers_out,
        active_subjects=active_subjects,
    )
