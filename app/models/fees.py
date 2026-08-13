from sqlalchemy import Column, Text, ForeignKey, TIMESTAMP, Date, Numeric, text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.enums import FeeProofStatus


class FeeVoucher(Base):
    __tablename__ = "fee_vouchers"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    student_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id"), nullable=False)
    amount = Column(Numeric, nullable=False)
    due_date = Column(Date, nullable=False)
    generated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    deleted_at = Column(TIMESTAMP(timezone=True))
    # No status column: derived at query time from the latest non-deleted fee_proofs row


class FeeProof(Base):
    __tablename__ = "fee_proofs"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    voucher_id = Column(UUID(as_uuid=True), ForeignKey("fee_vouchers.id"), nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    file_url = Column(Text, nullable=False)
    uploaded_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
    status = Column(FeeProofStatus, nullable=False, server_default="pending")
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at = Column(TIMESTAMP(timezone=True))
    rejection_reason = Column(Text)
    deleted_at = Column(TIMESTAMP(timezone=True))
