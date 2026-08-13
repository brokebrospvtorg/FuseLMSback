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
