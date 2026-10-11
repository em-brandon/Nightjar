"""Recording that a P1 was acknowledged on the phone, once, however it was learned.

Two paths find out: Pushover's callback (hub/pushover.py) and the receipt poll (hub/outbox.py), which also
covers a hub Pushover cannot reach. Both end here, so an acknowledgement is stamped on the alert and added
to the incident's audit trail exactly once, whichever hears first.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Issue, IssueAction, Notification


async def dismiss_alerts(session: AsyncSession, issue_id: int, now: datetime | None = None, except_id: int | None = None) -> int:
    """Mark the incident's sent alerts as dealt with, in the caller's transaction.

    Called wherever the hub changes an incident because someone, or the hub itself, has dealt with it:
    acknowledged, resolved, muted, snoozed, or resolved automatically. The sender then tells Pushover to stop
    any alert still repeating on the phone (hub/outbox.py `cancel_receipts`), and a P1 that expired
    unanswered stops needing anyone's attention. Written with the status change, so the two cannot disagree.
    Returns how many alerts were marked.
    """
    query = update(Notification).where(Notification.issue_id == issue_id, Notification.status == "sent", Notification.dismissed_at.is_(None))
    if except_id is not None:
        query = query.where(Notification.id != except_id)
    result = await session.execute(query.values(dismissed_at=now or datetime.now(UTC)))
    return result.rowcount or 0


async def record_acknowledgement(session: AsyncSession, note: Notification, when: datetime) -> bool:
    """Stamp `note` as acknowledged at `when` and acknowledge its incident. Returns False if it already
    was, in which case nothing is written. The caller commits."""
    if note.acked_at is not None:
        return False
    note.acked_at = when
    # Same lock ingest and the console's actions take, so this cannot interleave with an event
    issue = await session.scalar(select(Issue).where(Issue.id == note.issue_id).with_for_update())
    if issue is not None and issue.status == "open":
        session.add(IssueAction(issue_id=issue.id, action="ack", detail="phone"))
    # acknowledging one push on the phone does not stop another push for the same incident that is still repeating
    await dismiss_alerts(session, note.issue_id, when, except_id=note.id)
    return True
