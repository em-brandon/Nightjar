"""Recording that a P1 was acknowledged on the phone, once, however it was learned.

Two paths find out: Pushover's callback (hub/pushover.py) and the receipt poll (hub/outbox.py), which also
covers a hub Pushover cannot reach. Both end here, so an acknowledgement is stamped on the alert and added
to the incident's audit trail exactly once, whichever hears first.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Issue, IssueAction, Notification


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
    return True
