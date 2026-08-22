"""
Admin Operations > Password Requests.

Backend for the logged-out 'Request Password Reset from Admin' button
(app/routers/auth.py's POST /api/auth/request-password-reset-approval
creates the row) and the Admin review queue that acts on it. Kept as its
own top-level router — same reasoning as content.py's classroom_requests_
router being separate from the content router — since the audience here
(Admin reviewing a queue) is structurally different from both the
logged-out submitter and from app/routers/users.py's existing
POST /{user_id}/reset-password (an Admin/Coordinator directly resetting a
password for a user *they picked*, no request/approval queue involved).

Approve always resets to the same fixed onboarding password below, not an
Admin-chosen one — that's what distinguishes this from users.py's
AdminResetPasswordRequest path. Reusing that endpoint's semantics anyway:
must_change_password is forced True either way, so the fixed value here is
only ever a short-lived, single-use handoff — the account owner is
required to replace it with something only they know at their very next
login (see app/core/dependencies via mustChangePasswordGuard on the
frontend). Same bcrypt hashing as every other password write in this app;
nothing about this path stores or transmits the value in plaintext beyond
the one-time value itself.
"""
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.core.security import hash_password
from app.core.audit import log_action
from app.core.notifications import notify
from app.models import PasswordResetRequest, User, StudentProfile, TeacherProfile
from app.schemas.auth import PasswordResetRequestOut, PasswordResetRequestReview

router = APIRouter(
    prefix="/api/admin/password-reset-requests",
    tags=["password-reset-requests"],
    dependencies=[Depends(check_license)],
)

# Fixed temporary password every approved request is reset to. Deliberately
# NOT Admin-chosen (compare users.py's AdminResetPasswordRequest, which is)
# — this queue's whole point is a single predictable handoff value support
# staff can tell a locked-out user over the phone without looking anything
# up. must_change_password=True on every approval is what keeps this safe
# to be predictable: it is never the account's standing password, only a
# one-time credential good for exactly one login.
ADMIN_RESET_TEMP_PASSWORD = "Inkling@2026"


def _to_out(db: Session, req: PasswordResetRequest) -> PasswordResetRequestOut:
    out = PasswordResetRequestOut.model_validate(req)
    user = db.query(User).filter(User.id == req.user_id).first()
    if not user:
        return out
    out.user_name = user.full_name
    out.role = user.role
    if user.role == "student":
        profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
        out.roll_or_employee_id = profile.roll_number if profile else None
    elif user.role == "teacher":
        profile = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
        out.roll_or_employee_id = profile.teacher_code if profile else None
    return out


@router.get("", response_model=List[PasswordResetRequestOut])
def list_password_reset_requests(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """`status_filter` defaults to unset (every request, any status) so the
    same endpoint can later back a reviewed-history view — the Operations
    screen itself is expected to pass ?status_filter=pending."""
    query = db.query(PasswordResetRequest).filter(PasswordResetRequest.deleted_at.is_(None))
    if status_filter:
        if status_filter not in ("pending", "approved", "rejected"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status_filter")
        query = query.filter(PasswordResetRequest.status == status_filter)
    requests = query.order_by(PasswordResetRequest.created_at.desc()).all()
    return [_to_out(db, r) for r in requests]


@router.patch("/{request_id}/review", response_model=PasswordResetRequestOut)
def review_password_reset_request(
    request_id: uuid.UUID,
    payload: PasswordResetRequestReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    """'Approve Reset' — resets the target's password to the fixed
    ADMIN_RESET_TEMP_PASSWORD and forces must_change_password. 'Reject'
    closes the request out with no password change. Must currently be
    'pending' — reviewing an already-decided request 409s instead of
    silently re-applying (same duplicate-decision guard as content.py's
    classroom/YouTube request queues)."""
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be 'approved' or 'rejected'")

    req = db.query(PasswordResetRequest).filter(
        PasswordResetRequest.id == request_id, PasswordResetRequest.deleted_at.is_(None)
    ).first()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {req.status} — it can't be reviewed again.",
        )

    target = db.query(User).filter(User.id == req.user_id, User.deleted_at.is_(None)).first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The user this request belongs to no longer exists")

    req.status = payload.status
    req.reviewed_by = current_user.id
    req.review_note = payload.review_note
    req.reviewed_at = datetime.now(timezone.utc)

    if payload.status == "approved":
        target.password_hash = hash_password(ADMIN_RESET_TEMP_PASSWORD)
        target.must_change_password = True
        log_action(db, current_user.id, "password_reset_by_admin_request", "users", target.id, None,
                   {"request_id": str(req.id)})
        notify(
            db, target.id, "password_reset_approved",
            "Your password reset request was approved. Use the temporary password provided by "
            "your Admin to log in, then set a new password.",
            related_entity_type="password_reset_requests", related_entity_id=req.id,
        )
    else:
        notify(
            db, target.id, "password_reset_rejected",
            "Your password reset request was rejected."
            + (f" Note: {payload.review_note}" if payload.review_note else ""),
            related_entity_type="password_reset_requests", related_entity_id=req.id,
        )

    db.commit()
    db.refresh(req)
    return _to_out(db, req)
