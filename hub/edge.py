"""Edge-triggered problems, shared by every source that pushes state into the hub.

A source (staging stacks, the home media server, the home host) reports a snapshot every minute.
Two rules keep that from turning into noise, and they live here once so each source cannot get
them subtly different:

  * a problem opens an issue only when it STARTS, not on every report while it lasts. One per
    report would be 1,440 a day and would trip the spike detector;
  * it resolves itself only after it has stayed gone for the whole healthy window, with reports to
    prove it. See `_healthy_for` in stacks.py for why that is strict.
"""

import logging
from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .ingest import record_event
from .models import Issue, IssueAction, Site
from .schemas import EventIn
from .stacks import _fingerprint, _healthy_for

log = logging.getLogger("nightjar.edge")


async def open_new(
    session: AsyncSession, site: Site, new_keys: Iterable[str], make_event: Callable[[str], EventIn]
) -> None:
    """Record an event for each problem that has just started."""
    for key in sorted(new_keys):
        try:
            await record_event(session, site, make_event(key))
        except Exception:
            # The report is already saved. A failure to open an issue must not turn into a
            # failed report, and must not poison the session.
            log.exception("could not record %s", key)
            await session.rollback()


async def auto_resolve(
    session: AsyncSession,
    site: Site,
    keys: Iterable[str],
    make_event: Callable[[str], EventIn],
    timeline: list,
    now,
    still_bad: set[str],
    why: str,
) -> None:
    """Resolve the open issue for each problem that has stayed gone. Only `open` issues are
    touched (muted stays muted, snoozed stays snoozed), and each resolve is logged as automatic."""
    candidates = {key: _fingerprint(site, make_event(key)) for key in keys if key not in still_bad}
    if not candidates:
        return
    # the same row lock ingest and the console's actions take
    issues = (
        await session.scalars(
            select(Issue)
            .where(
                Issue.site_id == site.id,
                Issue.fingerprint.in_(list(candidates.values())),
                Issue.status == "open",
            )
            .with_for_update()
        )
    ).all()
    by_fp = {i.fingerprint: i for i in issues}
    changed = False
    for key, digest in candidates.items():
        issue = by_fp.get(digest)
        if issue is not None and _healthy_for(key, timeline, now):
            issue.status = "resolved"
            issue.snoozed_until = None
            session.add(
                IssueAction(
                    issue_id=issue.id,
                    action="resolve",
                    detail=f"auto: {why} for {settings.stack_healthy_min} minutes",
                )
            )
            changed = True
    if changed:
        await session.commit()
    else:
        await session.rollback()  # release the row locks
