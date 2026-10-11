"""The alert outbox: writing a notification when a state change happens.

`enqueue` runs inside the transaction that records the change (see ingest.record_event), so either the
change and its notification both exist or neither does. A restart between "noticed a problem" and "told
you" therefore loses nothing. The sender loop that delivers the rows is added beside this (decision #56).

A muted issue writes no row, and a P3 writes none either (it goes to the morning digest). A retried
ingest cannot write a second row for the same change: the table has a unique key on the issue, the kind
of change and when it happened, and the insert does nothing on a conflict.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import notify
from .config import settings
from .models import Issue, Notification
from .policy import ALERTING_CHANGES, decide_safely
from .presence import effective_state

log = logging.getLogger("nightjar.outbox")


def window_start(now: datetime, minutes: int) -> datetime:
    """The start of the `minutes`-long window `now` falls in, aligned to the clock. A spike's time of
    change is this, so a spike that lasts for ten windows is ten alerts and not one per event."""
    step = timedelta(minutes=minutes)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((now - epoch) // step) * step


async def enqueue(
    session: AsyncSession,
    issue: Issue,
    site_name: str,
    change: str | None,
    changed_at: datetime,
    now: datetime,
) -> bool:
    """Write the notification for one state change in the caller's transaction, if one is due.
    Returns whether a new row was written."""
    if change not in ALERTING_CHANGES or issue.status == "muted":
        return False

    presence = await effective_state(session, now)
    decision = decide_safely(issue.severity, change, presence, now)
    if not decision.push:
        return False

    result = await session.execute(
        insert(Notification)
        .values(
            issue_id=issue.id,
            change=change,
            changed_at=changed_at,
            severity=issue.severity,
            priority=decision.priority,
            speaker=decision.speaker,
            site=site_name[:120],
            title=issue.title[:300],
        )
        .on_conflict_do_nothing(constraint="uq_notification_change")
    )
    return result.rowcount == 1


# ---------------------------------------------------------------- the sender

SendFn = Callable[[Notification], Awaitable[notify.Sent]]


def backoff(attempts: int) -> timedelta:
    """How long to wait after the `attempts`-th failed attempt. Past the end of the list the last delay repeats."""
    delays = settings.outbox_backoff_s or [60]
    return timedelta(seconds=delays[min(attempts, len(delays)) - 1])


async def claim_due(maker: async_sessionmaker[AsyncSession], now: datetime, limit: int = 10) -> list[Notification]:
    """Take up to `limit` rows that are due and mark them "sending", in one short transaction.

    FOR UPDATE SKIP LOCKED means two senders never take the same row, so a second process is safe. The
    claim is committed before anything is sent: no lock is held across a network call. A row left
    "sending" by a crash is taken again once its claim is older than the lease, which is what makes
    delivery at-least-once and never at-most-once.
    """
    stale = now - timedelta(seconds=settings.outbox_lease_s)
    async with maker() as session:
        rows = (
            await session.scalars(
                select(Notification)
                .where(
                    or_(
                        (Notification.status == "pending") & (Notification.next_attempt_at <= now),
                        (Notification.status == "sending") & (Notification.claimed_at <= stale),
                    )
                )
                .order_by(Notification.priority.desc(), Notification.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            row.status = "sending"
            row.claimed_at = now
            row.attempts += 1
        await session.commit()
        return list(rows)


async def _finish(maker: async_sessionmaker[AsyncSession], n: Notification, now: datetime, error: str | None, retryable: bool, sent: notify.Sent | None) -> None:
    values: dict = {"claimed_at": None}
    if sent is not None:
        values.update(status="sent", sent_at=now, receipt=sent.receipt, last_error=None)
    elif retryable and n.attempts < settings.outbox_max_attempts:
        values.update(status="pending", next_attempt_at=now + backoff(n.attempts), last_error=error)
    else:
        # Out of attempts, or a failure no retry can fix. It stays here, and the console shows it.
        values.update(status="failed", last_error=error)
    async with maker() as session:
        await session.execute(update(Notification).where(Notification.id == n.id, Notification.status == "sending").values(**values))
        await session.commit()


async def deliver(maker: async_sessionmaker[AsyncSession], n: Notification, send: SendFn, now: datetime) -> str:
    """Send one claimed row and record what happened. Returns the row's new status."""
    try:
        sent = await send(n)
    except notify.NotConfigured as err:
        await _finish(maker, n, now, str(err)[:300], False, None)
        return "failed"
    except notify.SendError as err:
        await _finish(maker, n, now, str(err)[:300], err.retryable, None)
        return "pending" if err.retryable and n.attempts < settings.outbox_max_attempts else "failed"
    except Exception as err:
        # A bug in the adapter must not wedge a row in "sending": it is retried like any other failure.
        log.exception("unexpected error sending notification %s", n.id)
        await _finish(maker, n, now, f"unexpected {type(err).__name__}", True, None)
        return "pending" if n.attempts < settings.outbox_max_attempts else "failed"
    await _finish(maker, n, now, None, False, sent)
    return "sent"


async def run_once(maker: async_sessionmaker[AsyncSession], send: SendFn = notify.send, now: datetime | None = None, limit: int = 10) -> int:
    """One pass: claim what is due and deliver it. Returns how many rows were handled."""
    now = now or datetime.now(UTC)
    rows = await claim_due(maker, now, limit)
    for n in rows:
        await deliver(maker, n, send, now)
    return len(rows)
