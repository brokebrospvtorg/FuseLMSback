import uuid
from typing import List, Optional

from pydantic import BaseModel

from app.schemas.common import BoardEnum


class TeacherWorkloadLevelOut(BaseModel):
    level_id: uuid.UUID
    level_name: str


class TeacherWorkloadAssignmentOut(BaseModel):
    """One active (non-deleted) teacher_subject_assignments row, joined with
    display names — same enrichment pattern as TeacherAssignmentRegistryOut
    in app/schemas/academic.py."""
    subject_id: uuid.UUID
    subject_name: str
    batch_id: uuid.UUID
    batch_name: str


class TeacherWorkloadSummaryOut(BaseModel):
    """One row of GET /api/teachers/workload-summary — powers the
    Admin/Coordinator Portal's Teachers sidebar section list view."""
    id: uuid.UUID
    full_name: str
    email: str
    teacher_code: Optional[str] = None
    phone_number: Optional[str] = None
    boards: List[BoardEnum] = []
    levels: List[TeacherWorkloadLevelOut] = []
    assignments: List[TeacherWorkloadAssignmentOut] = []
    # Convenience counts so the sidebar's badge/summary chips don't need to
    # len() these lists client-side.
    active_subjects_count: int = 0
    active_batches_count: int = 0

    class Config:
        from_attributes = True
