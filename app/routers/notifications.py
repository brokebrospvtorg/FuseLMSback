import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.limiter import limiter
from app.core.notifications import notify
from app.models import Notification, User
from app.schemas.communication import NotificationOut
from app.schemas.notifications import BroadcastNotificationCreate, BroadcastResult

router = APIRouter(prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(check_license)])


@router.get("", response_model=List[NotificationOut])
def list_my_notifications(unread_only: bool = False, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    query = db.query(Notification).filter(
        Notification.user_id == current_user.id, Notification.deleted_at.is_(None)
    )
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    return query.order_by(Notification.created_at.desc()).all()


@router.patch("/{notification_id}/read", response_model=NotificationOut)
def mark_read(notification_id: uuid.UUID, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    notif = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == current_user.id,
        Notification.deleted_at.is_(None),
    ).first()
    if not notif:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    notif.read_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(notif)
    return notif


@router.post("/broadcast", response_model=BroadcastResult, status_code=status.HTTP_201_CREATED)
@limiter.limit("2/minute")
def broadcast_notification(request: Request, payload: BroadcastNotificationCreate, db: Session = Depends(get_db),
                            current_user: User = Depends(require_roles("admin"))):
    """
    Admin Sub-Sprint 4: "Broadcast system notifications and fee alerts."
    Every Notification row before this was created by notify() as a side
    effect of a specific action (grade override, fee approval, etc) — this
    is the first one-to-many path: Admin picks a role (or "everyone") and
    the same message goes out as individual Notification rows, one per
    recipient, through the same shared notify() helper everything else
    uses (so it shows up in each person's bell/list identically — no
    separate "announcements" table or code path to keep in sync).
    In-app only by design: broadcasting to potentially the whole school
    through the (still-a-console-log) email stub isn't useful yet, and
    real delivery is explicitly deferred past this sprint.
    """
    query = db.query(User).filter(User.deleted_at.is_(None), User.status == "active")
    if payload.role:
        query = query.filter(User.role == payload.role)
    recipients = query.all()

    for user in recipients:
        notify(db, user.id, "broadcast", payload.message)
    db.commit()

    return BroadcastResult(recipient_count=len(recipients))
