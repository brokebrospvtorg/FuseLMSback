import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.jobs import (
    generate_fee_vouchers, cleanup_expired_tokens, cleanup_old_fee_proofs, expire_ended_batches,
)

logger = logging.getLogger("fuse_lms.scheduler")

scheduler = BackgroundScheduler(timezone="UTC")


def start_scheduler() -> None:
    if not settings.ENABLE_SCHEDULER:
        logger.info("Scheduler disabled via ENABLE_SCHEDULER=false")
        return

    scheduler.add_job(
        cleanup_expired_tokens,
        trigger=CronTrigger(hour=settings.TOKEN_CLEANUP_CRON_HOUR, minute=0),
        id="cleanup_expired_tokens",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        generate_fee_vouchers,
        trigger=CronTrigger(day=settings.FEE_VOUCHER_CRON_DAY, hour=settings.FEE_VOUCHER_CRON_HOUR, minute=0),
        id="generate_fee_vouchers",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        cleanup_old_fee_proofs,
        trigger=CronTrigger(hour=settings.FEE_PROOF_RETENTION_CRON_HOUR, minute=0),
        id="cleanup_old_fee_proofs",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        expire_ended_batches,
        trigger=CronTrigger(hour=settings.BATCH_EXPIRY_CRON_HOUR, minute=0),
        id="expire_ended_batches",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: cleanup_expired_tokens (daily), "
        "generate_fee_vouchers (monthly), cleanup_old_fee_proofs (daily), "
        "expire_ended_batches (daily)"
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
