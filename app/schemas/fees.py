import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class FeeVoucherCreate(BaseModel):
    student_id: uuid.UUID
    batch_id: uuid.UUID
    amount: Decimal
    due_date: date


class FeeVoucherOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    student_full_name: str
    batch_id: uuid.UUID
    amount: Decimal
    due_date: date
    generated_at: datetime
    status: str  # pending | submitted | paid | rejected — matches frontend DerivedVoucherStatus
    latest_proof_id: Optional[uuid.UUID] = None  # what Approve/Reject actually act on
    # Display-only invoice code (e.g. "INV-2026-3F9A21C4") for the Generate
    # Fee Bill feature — NOT a stored column. fee_vouchers has no
    # voucher-number field, so this is derived deterministically from the
    # voucher's own id + generation year at read time (see
    # routers/fees.py::_voucher_number). Stable for a given voucher, unique
    # across vouchers, and needs no migration or sequence table.
    voucher_number: str = ""

    class Config:
        from_attributes = True


class FeeProofReview(BaseModel):
    status: str  # approved | rejected
    rejection_reason: Optional[str] = None


class FeeProofOut(BaseModel):
    id: uuid.UUID
    voucher_id: uuid.UUID
    uploaded_by: uuid.UUID
    file_url: str
    uploaded_at: datetime
    status: str
    reviewed_by: Optional[uuid.UUID]
    reviewed_at: Optional[datetime]
    rejection_reason: Optional[str]

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Fee Structures (Admin Sub-Sprint 4: "set subject/student fee bills using
# preset layouts"). NULL student_id = subject-wide default; a non-NULL
# student_id is a per-student override. The two partial unique indexes on
# fee_structures (see schema_update.sql) already enforce "only one active
# default per subject" and "only one active override per student+subject"
# at the DB level — the router below relies on those rather than
# re-checking uniqueness itself.
# ---------------------------------------------------------------------------
class FeeStructureCreate(BaseModel):
    subject_id: uuid.UUID
    student_id: Optional[uuid.UUID] = None  # None = subject-wide default
    amount: Decimal


class FeeStructureOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: Optional[str] = None
    student_id: Optional[uuid.UUID] = None
    student_name: Optional[str] = None
    amount: Decimal
    set_by: uuid.UUID
    created_at: datetime

    class Config:
        from_attributes = True


class FeeStructureAmountUpdate(BaseModel):
    amount: Decimal
