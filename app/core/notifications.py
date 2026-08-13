"""
Shared notification-writing helper.

Sprint 4 finding: the `notifications` table and the list/mark-read endpoints
existed, but no code path ever inserted a row into it. This module is the
one place that does — every trigger (grade override, fee proof review,
subject request submitted, etc.) should call `notify()` instead of
constructing a Notification row inline, so all notifications behave
consistently and there's a single place to change if the shape ever changes.

Per Sprint 4 scope: real email delivery is deferred. `send_email()` stays the
console-print stub — this helper still calls it (so the notification's
"email side" is exercised end-to-end against the stub), but doesn't wire up
any real SMTP/API integration.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Notification
from app.utils.email import send_email


def notify(
    db: Session,
    user_id: uuid.UUID,
    type: str,
    message: str,
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[uuid.UUID] = None,
    email_to: Optional[str] = None,
    email_subject: Optional[str] = None,
) -> Notification:
    """Adds a Notification row to the session (does NOT commit — the caller
    commits as part of its own transaction, so the notification and the
    action that triggered it succeed or fail together).

    If email_to is given, channel is 'both' and the (stub) send_email() is
    fired immediately with sent_at set; otherwise channel is 'in_app' only.
    """
    channel = "both" if email_to else "in_app"
    notif = Notification(
        user_id=user_id,
        type=type,
        message=message,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        channel=channel,
        sent_at=datetime.now(timezone.utc) if email_to else None,
    )
    db.add(notif)

    if email_to:
        send_email(email_to, email_subject or type, message)

    return notif
