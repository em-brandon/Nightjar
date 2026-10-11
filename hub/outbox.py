"""The alert outbox: writing a notification when a state change happens.

`enqueue` runs inside the transaction that records the change (see ingest.record_event), so either the
change and its notification both exist or neither does. A restart between "noticed a problem" and "told
you" therefore loses nothing. The sender loop that delivers the rows is added beside this (decision #56).

A muted issue writes no row, and a P3 writes none either (it goes to the morning digest). A retried
ingest cannot write a second row for the same change: the table has a unique key on the issue, the kind
of change and when it happened, and the insert does nothing on a conflict.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import notify
from .acks import record_acknowledgement
from .api import acknowledged_ids
from .config import settings
from .db import SessionLocal
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


async def _already_handled(session: AsyncSession, issue_id: int) -> bool:
    """Whether someone has dealt with the incident since: it is no longer open, or it has been acknowledged."""
    issue = await session.get(Issue, issue_id)
    if issue is None or issue.status != "open":
        return True
    return issue.id in await acknowledged_ids(session, [issue.id])


async def _finish(maker: async_sessionmaker[AsyncSession], n: Notification, now: datetime, error: str | None, retryable: bool, sent: notify.Sent | None) -> None:
    values: dict = {"claimed_at": None}
    if sent is not None:
        values.update(status="sent", sent_at=now, receipt=sent.receipt, last_error=None)
        # The incident may have been acknowledged or resolved between queueing this and sending it. The push is
        # already on its way and will repeat, so it is dismissed at once and the cancel pass stops it.
        if sent.receipt:
            async with maker() as check:
                if await _already_handled(check, n.issue_id):
                    values["dismissed_at"] = now
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


# ---------------------------------------------------------------- receipts

CheckFn = Callable[[str], Awaitable[notify.ReceiptState]]
RECEIPT_BATCH = 10
# A receipt this old has long since settled either way: stop asking about it.
RECEIPT_GIVE_UP = timedelta(hours=24)


async def reconcile_receipts(maker: async_sessionmaker[AsyncSession], check: CheckFn = notify.check_receipt, now: datetime | None = None) -> int:
    """Ask Pushover about priority 2 pushes that are neither acknowledged nor known to have expired.

    Two things are learned. An acknowledgement the callback never delivered (a hub Pushover cannot reach,
    as on a laptop) is recorded the same way the callback would. And a receipt that expired with nobody
    acknowledging it is stamped, because that is a P1 that did not get a human and the console must say
    so. A failed check changes nothing and is tried again on the next pass. Returns how many changed.
    """
    now = now or datetime.now(UTC)
    changed = 0
    async with maker() as session:
        due = (
            await session.scalars(
                select(Notification)
                .where(
                    Notification.status == "sent",
                    Notification.receipt.is_not(None),
                    Notification.acked_at.is_(None),
                    Notification.expired_at.is_(None),
                    # once someone has dealt with the incident, an expiry is no longer news
                    Notification.dismissed_at.is_(None),
                    Notification.sent_at >= now - RECEIPT_GIVE_UP,
                )
                .order_by(Notification.sent_at)
                .limit(RECEIPT_BATCH)
            )
        ).all()
        for note in due:
            try:
                state = await check(note.receipt)
            except notify.NotConfigured:
                break
            except notify.SendError as err:
                log.warning("receipt check failed for notification %s: %s", note.id, err)
                continue
            if state.acknowledged:
                changed += await record_acknowledgement(session, note, state.acknowledged_at or now)
            elif state.expired:
                note.expired_at = now
                changed += 1
        await session.commit()
    return changed


# ---------------------------------------------------------------- cancelling

CancelFn = Callable[[str], Awaitable[notify.Cancelled]]
CANCEL_BATCH = 10


async def cancel_receipts(maker: async_sessionmaker[AsyncSession], cancel: CancelFn = notify.cancel_receipt, now: datetime | None = None) -> int:
    """Tell Pushover to stop repeating pushes for incidents someone has dealt with.

    A push is cancelled once it is sent, has a receipt, and its alert was dismissed (acknowledged, resolved, muted
    or snoozed since), unless it was already acknowledged on the phone or has expired. The dismissal is written
    with the action itself, so this pass is what makes it durable: a failed call is tried again on the next pass,
    and a phone is never left alarming for something already handled. A refusal of the receipt is a settled
    answer (it had expired, or been acknowledged) and is recorded as cancelled so it is not asked about again.
    Returns how many were settled.
    """
    now = now or datetime.now(UTC)
    settled = 0
    async with maker() as session:
        due = (
            await session.scalars(
                select(Notification)
                .where(
                    Notification.status == "sent",
                    Notification.receipt.is_not(None),
                    Notification.dismissed_at.is_not(None),
                    Notification.cancelled_at.is_(None),
                    Notification.acked_at.is_(None),
                    Notification.expired_at.is_(None),
                    Notification.sent_at >= now - RECEIPT_GIVE_UP,
                )
                .order_by(Notification.dismissed_at)
                .limit(CANCEL_BATCH)
            )
        ).all()
        for note in due:
            try:
                await cancel(note.receipt)
            except notify.NotConfigured:
                break
            except notify.SendError as err:
                log.warning("could not cancel the push for notification %s yet: %s", note.id, err)
                continue
            note.cancelled_at = now
            settled += 1
        await session.commit()
    return settled


# ---------------------------------------------------------------- the loop

BATCH = 10


class SenderState:
    """What the sender last did, for the console's status panel. In memory and per process, which is
    right because the hub is one process (CLAUDE.md); the external dead-man's switch is what notices a
    hub that is not running at all."""

    last_pass_at: datetime | None = None
    last_error: str | None = None


sender_state = SenderState()


class Heartbeat:
    """Pings the external dead-man's switch, but only when asked after a healthy pass, and no more often
    than `deadman_interval_s`. A failed ping is logged by class name (the URL is a secret) and retried on
    the next pass: the switch being down must never stop alerts from being sent."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.last: float | None = None

    async def beat(self, now: float) -> bool:
        if not settings.deadman_url:
            return False
        if self.last is not None and now - self.last < settings.deadman_interval_s:
            return False
        try:
            res = await self.http.get(settings.deadman_url)
            res.raise_for_status()
        except httpx.HTTPError as err:
            log.warning("dead-man's switch ping failed (%s)", type(err).__name__)
            return False
        self.last = now
        return True


async def sender_loop() -> None:
    """Runs for the life of the app, like the probe. Nothing in here may escape and kill it: a sender
    that dies quietly looks exactly like a night with nothing to report. The dead-man's switch is pinged
    only after a pass that completed, so a sender that has died or is stuck stops pinging and gets noticed."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        heartbeat = Heartbeat(http)
        last_receipts = 0.0
        while True:
            handled = 0
            healthy = False
            try:
                handled = await run_once(SessionLocal, limit=BATCH)
                healthy = True
                sender_state.last_pass_at = datetime.now(UTC)
                sender_state.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.exception("outbox pass failed; will retry")
                sender_state.last_error = type(err).__name__
            if healthy:
                await heartbeat.beat(time.monotonic())
            # Cancelling is time-sensitive (the phone is alarming), so it runs every pass, not on the receipt schedule.
            if healthy and notify.configured():
                try:
                    await cancel_receipts(SessionLocal)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("cancel pass failed; will retry")
            # Receipts are asked about on their own, gentler schedule, and a failure here never stops sending.
            if notify.configured() and time.monotonic() - last_receipts >= settings.receipt_poll_s:
                last_receipts = time.monotonic()
                try:
                    await reconcile_receipts(SessionLocal)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("receipt check pass failed; will retry")
            # a full batch means there may be more waiting, so do not sleep
            await asyncio.sleep(0 if handled >= BATCH else settings.outbox_poll_s)
