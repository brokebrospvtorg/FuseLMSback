from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.jobs import generate_fee_vouchers, cleanup_expired_tokens
from app.models import SystemSettings, User
from app.schemas.system import SystemSettingsOut, SystemSettingsUpdate

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/settings", response_model=SystemSettingsOut)
def get_settings(db: Session = Depends(get_db), current_user: User = Depends(require_roles("admin"))):
    return db.query(SystemSettings).filter(SystemSettings.id == 1).first()


@router.patch("/settings", response_model=SystemSettingsOut)
def update_settings(payload: SystemSettingsUpdate, db: Session = Depends(get_db),
                     current_user: User = Depends(require_roles("admin"))):
    """
    Admin-only. NOTE: license_expiry_date is normally set by the developer
    (Account B), not exposed to the school in the real deployment — this
    endpoint exists for the developer's own admin tooling.
    """
    settings_row = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if payload.license_expiry_date is not None:
        settings_row.license_expiry_date = payload.license_expiry_date
    if payload.school_name is not None:
        settings_row.school_name = payload.school_name
    db.commit()
    db.refresh(settings_row)
    return settings_row


# ---------------------------------------------------------------------------
# Manual triggers for the two scheduled jobs (app/core/jobs.py). Both run on
# their own cron schedule already (see app/core/scheduler.py) — these exist
# so an Admin can run them on demand instead of waiting for the schedule,
# e.g. right after go-live or while testing.
# ---------------------------------------------------------------------------
@router.post("/jobs/generate-fee-vouchers")
def trigger_fee_voucher_generation(current_user: User = Depends(require_roles("admin"))):
    return generate_fee_vouchers()


@router.post("/jobs/cleanup-expired-tokens")
def trigger_token_cleanup(current_user: User = Depends(require_roles("admin"))):
    return cleanup_expired_tokens()
