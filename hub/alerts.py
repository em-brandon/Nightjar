"""The alert outbox as the console sees it.

An alert that could not be sent must never look like an alert that was not needed, so every notification
is listed here with what happened to it, and `undelivered` is the set the console must not let anyone
miss: rows that failed for good, and rows that have already failed once and are being retried.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import notify, outbox
from .config import settings
from .db import get_session
from .models import Notification
from .schemas import AlertsStatusOut, NotificationOut, TestPushOut

router = APIRouter(prefix="/api/notifications", tags=["alerts"])


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    status: Literal["all", "failed", "undelivered", "attention"] = "all",
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[NotificationOut]:
    """Newest first. `failed` is what gave up. `undelivered` is that plus anything that has already
    failed once and is still being retried, so a flaky provider shows before it exhausts its attempts.
    `attention` is everything the console must not let anyone miss: undelivered, plus a P1 whose receipt
    expired with nobody acknowledging it."""
    query = select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    if status == "failed":
        query = query.where(Notification.status == "failed")
    elif status in ("undelivered", "attention"):
        conditions = [
            Notification.status == "failed",
            (Notification.status.in_(("pending", "sending"))) & (Notification.attempts > 0),
        ]
        if status == "attention":
            # an expired P1 stops needing attention once someone has dealt with the incident since
            conditions.append(Notification.expired_at.is_not(None) & Notification.acked_at.is_(None) & Notification.dismissed_at.is_(None))
        query = query.where(or_(*conditions))
    return [NotificationOut.model_validate(n, from_attributes=True) for n in await session.scalars(query)]


@router.get("/status", response_model=AlertsStatusOut)
async def status(session: AsyncSession = Depends(get_session)) -> AlertsStatusOut:
    """Whether the alert path can work, and what the sender is doing: the answer to "would a P1 reach
    my phone right now?" without having to cause one."""
    counts = dict(
        (await session.execute(select(Notification.status, func.count()).group_by(Notification.status))).all()
    )
    retrying = await session.scalar(
        select(func.count()).where(Notification.status.in_(("pending", "sending")), Notification.attempts > 0)
    )
    sent_24h = await session.scalar(
        select(func.count()).where(Notification.status == "sent", Notification.sent_at >= datetime.now(UTC) - timedelta(hours=24))
    )
    return AlertsStatusOut(
        pushover_configured=notify.configured(),
        callback_configured=notify.callback_url() is not None,
        deadman_configured=bool(settings.deadman_url),
        sender_enabled=settings.outbox_enabled,
        sender_poll_s=settings.outbox_poll_s,
        sender_last_pass_at=outbox.sender_state.last_pass_at,
        sender_last_error=outbox.sender_state.last_error,
        pending=(counts.get("pending", 0) + counts.get("sending", 0)) - (retrying or 0),
        retrying=retrying or 0,
        failed=counts.get("failed", 0),
        sent_24h=sent_24h or 0,
    )


@router.post("/test-push", response_model=TestPushOut)
async def test_push() -> TestPushOut:
    """Send one real push through the real adapter, to prove the phone path works. It is not queued and
    belongs to no incident, so it cannot be mistaken for an alert, and it always answers 200 with what
    happened: the reason a push failed is the thing the screen needs to show."""
    note = Notification(severity="P3", change="test", site="Nightjar", title="Test push. If you can read this on your phone, alerts will reach you.", priority=0)
    try:
        await notify.send(note)
    except notify.NotConfigured as err:
        return TestPushOut(sent=False, error=str(err), retryable=False)
    except notify.SendError as err:
        return TestPushOut(sent=False, error=str(err), retryable=err.retryable)
    return TestPushOut(sent=True)


@router.post("/{notification_id:int}/retry", response_model=NotificationOut)
async def retry(notification_id: int, session: AsyncSession = Depends(get_session)) -> NotificationOut:
    """Give a failed alert a fresh set of attempts, for example after fixing the token. Only a failed one
    can be retried: a pending one is already being retried, and a sent one must not be sent twice."""
    note = await session.scalar(select(Notification).where(Notification.id == notification_id).with_for_update())
    if note is None:
        raise HTTPException(404, "unknown notification")
    if note.status != "failed":
        raise HTTPException(409, f"only a failed alert can be retried ({note.status})")
    note.status = "pending"
    note.attempts = 0
    note.next_attempt_at = datetime.now(UTC)
    note.claimed_at = None
    note.last_error = None
    await session.commit()
    return NotificationOut.model_validate(note, from_attributes=True)
