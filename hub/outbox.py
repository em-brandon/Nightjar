"""The alert outbox: writing a notification when a state change happens.

`enqueue` runs inside the transaction that records the change (see ingest.record_event), so either the
change and its notification both exist or neither does. A restart between "noticed a problem" and "told
you" therefore loses nothing. The sender loop that delivers the rows is added beside this (decision #56).

A muted issue writes no row, and a P3 writes none either (it goes to the morning digest). A retried
ingest cannot write a second row for the same change: the table has a unique key on the issue, the kind
of change and when it happened, and the insert does nothing on a conflict.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Issue, Notification
from .policy import ALERTING_CHANGES, decide_safely
from .presence import effective_state


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
