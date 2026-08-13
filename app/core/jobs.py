"""
Scheduled jobs.

Both jobs run against their own short-lived DB session (they're not
request-scoped) and are registered with APScheduler in app/core/scheduler.py.
Each is also exposed as a protected Admin-only manual-trigger endpoint in
app/routers/system.py, so you can run them on demand instead of waiting for
the cron schedule (useful for testing, or a first run right after go-live).
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.file_storage import delete_fee_proof_file
from app.models import Batch, Enrollment, FeeProof, FeeVoucher, VerificationToken

logger = logging.getLogger("fuse_lms.jobs")


def generate_fee_vouchers() -> dict:
    """
    Auto-generates one fee_voucher per student per the CURRENT batch, for
    every student who has at least one active, non-deleted enrollment in
    that batch and doesn't already have a (non-deleted) voucher for it.

    fee_vouchers has no status column by design — its status is always
    derived at read-time from the latest fee_proofs row (see routers/fees.py).
    """
    db = SessionLocal()
    created_count = 0
    try:
        current_batch = db.query(Batch).filter(
            Batch.is_current.is_(True), Batch.deleted_at.is_(None)
        ).first()
        if not current_batch:
            logger.warning("generate_fee_vouchers: no current batch set, skipping")
            return {"created": 0, "reason": "no current batch"}

        student_ids = {
            row.student_id for row in db.query(Enrollment.student_id).filter(
                Enrollment.batch_id == current_batch.id,
                Enrollment.status == "active",
                Enrollment.deleted_at.is_(None),
            ).all()
        }

        already_vouchered = {
            row.student_id for row in db.query(FeeVoucher.student_id).filter(
                FeeVoucher.batch_id == current_batch.id,
                FeeVoucher.deleted_at.is_(None),
            ).all()
        }

        due_date = (datetime.now(timezone.utc) + timedelta(days=settings.FEE_VOUCHER_DUE_DAYS)).date()

        for student_id in student_ids - already_vouchered:
            db.add(FeeVoucher(
                student_id=student_id,
                batch_id=current_batch.id,
                amount=Decimal(str(settings.FEE_VOUCHER_DEFAULT_AMOUNT)),
                due_date=due_date,
            ))
            created_count += 1

        db.commit()
        logger.info(f"generate_fee_vouchers: created {created_count} voucher(s) for batch {current_batch.name}")
        return {"created": created_count, "batch": current_batch.name}
    except Exception:
        db.rollback()
        logger.exception("generate_fee_vouchers failed")
        raise
    finally:
        db.close()


def cleanup_expired_tokens() -> dict:
    """
    verification_tokens is the one explicitly non-critical/transient table:
    rows are marked used_at (never hard-deleted) at use-time, and only
    HARD-deleted here, after a 30-day grace period, once either:
      - used_at is set (token was consumed) and is older than the grace period, OR
      - the token was never used and expires_at is older than the grace period.
    audit_logs is never touched by any cleanup job — that table is permanent.
    """
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.TOKEN_CLEANUP_RETENTION_DAYS)

        used_and_old = db.query(VerificationToken).filter(
            VerificationToken.used_at.isnot(None),
            VerificationToken.used_at < cutoff,
        )
        never_used_and_expired_long_ago = db.query(VerificationToken).filter(
            VerificationToken.used_at.is_(None),
            VerificationToken.expires_at < cutoff,
        )

        used_count = used_and_old.count()
        expired_count = never_used_and_expired_long_ago.count()

        used_and_old.delete(synchronize_session=False)
        never_used_and_expired_long_ago.delete(synchronize_session=False)

        db.commit()
        total = used_count + expired_count
        logger.info(f"cleanup_expired_tokens: hard-deleted {total} token(s)")
        return {"deleted": total, "used_and_old": used_count, "expired_unused": expired_count}
    except Exception:
        db.rollback()
        logger.exception("cleanup_expired_tokens failed")
        raise
    finally:
        db.close()


def cleanup_old_fee_proofs() -> dict:
    """
    Retention policy for fee proof images/PDFs, per the local-disk storage
    decision (no external storage provider).

    For every non-deleted fee_proofs row uploaded more than
    FEE_PROOF_RETENTION_DAYS ago and not already purged:
      - delete the physical file from local disk
      - overwrite file_url with the placeholder text

    Deliberately does NOT touch the row itself or its status/reviewed_by/
    reviewed_at/rejection_reason — those stay as the permanent record of
    what was reviewed and decided, per the "database text integrity"
    ground rule. Only the binary and its file_url path are purged. Like
    the other two jobs, this runs on its own short-lived session and is
    not user-triggered, so it does not write to audit_logs (audit_logs.
    user_id is NOT NULL and there's no logged-in actor here) — same
    reasoning as generate_fee_vouchers/cleanup_expired_tokens above.

    NOTE: this does not distinguish approved ("Paid") proofs from others —
    a proof's underlying image is purged 90 days after upload regardless
    of review outcome. If the school needs to keep approved payment proof
    images longer than 90 days for financial reconciliation or a tax/audit
    requirement, that's worth confirming before this goes live — the
    review decision (status/reviewer/reason) survives either way, but the
    actual image would not.
    """
    db = SessionLocal()
    purged_count = 0
    missing_on_disk_count = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.FEE_PROOF_RETENTION_DAYS)

        stale_proofs = db.query(FeeProof).filter(
            FeeProof.uploaded_at < cutoff,
            FeeProof.file_url != settings.FEE_PROOF_RETENTION_PLACEHOLDER,
            FeeProof.deleted_at.is_(None),
        ).all()

        for proof in stale_proofs:
            removed = delete_fee_proof_file(proof.file_url)
            if not removed:
                missing_on_disk_count += 1
            proof.file_url = settings.FEE_PROOF_RETENTION_PLACEHOLDER
            purged_count += 1

        db.commit()
        logger.info(
            f"cleanup_old_fee_proofs: purged {purged_count} file(s) "
            f"({missing_on_disk_count} already missing on disk)"
        )
        return {"purged": purged_count, "already_missing_on_disk": missing_on_disk_count}
    except Exception:
        db.rollback()
        logger.exception("cleanup_old_fee_proofs failed")
        raise
    finally:
        db.close()
