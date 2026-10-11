"""A site that stops answering becomes an issue, and so an alert.

Until now the probe only changed a site's colour in the console: a downed site could not wake the phone,
though an uptime failure is the headline P1 (CLAUDE.md). This turns an outage into a normal issue through
`record_event`, so it is fingerprinted, deduplicated, alerted on and acknowledged like any other.

Two rules, the same two every pushed source follows (hub/edge.py):

  * **Edge-triggered.** An issue opens when an outage STARTS, at the second failed check in a row (the
    same rule the console uses for "down"), and not on every failed check while it lasts. One event per
    minute would be 1,440 a day.
  * **It resolves itself only after a run of healthy checks**, `uptime_recover_checks` in a row, so a
    site that flaps does not open and close an issue all day. Every relapse after a resolve is a
    regression and an alert, so resolving early would be noisy and resolving never would hide recovery.

A production site's outage is P1 and wakes the phone. A staging site's is P2: nothing is down for a client.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .acks import dismiss_alerts
from .config import settings
from .ingest import record_event
from .models import Check, Issue, IssueAction, Site
from .schemas import EventIn
from .stacks import _fingerprint

log = logging.getLogger("nightjar.uptime")

# The message is fixed, never the URL or the status, so every outage of one site is one issue.
EXCEPTION_TYPE = "UptimeCheckFailed"
MESSAGE = "the site is not responding to uptime checks"


def outage_started(oks: list[bool]) -> bool:
    """`oks` is the site's recent checks, newest first. An outage starts at the second failure in a row:
    the newest two failed and the one before them, if there is one, did not."""
    return len(oks) >= 2 and not oks[0] and not oks[1] and (len(oks) < 3 or oks[2])


def recovered(oks: list[bool], needed: int) -> bool:
    """The newest `needed` checks all passed."""
    return len(oks) >= needed and all(oks[:needed])


def make_event(site: Site, last_error: str | None) -> EventIn:
    return EventIn(
        exception_type=EXCEPTION_TYPE,
        message=MESSAGE,
        level="fatal" if site.env == "production" else "error",
        environment=site.env,
        # what the last check saw, kept out of the message so it cannot split one outage into many issues
        extra={"last_error": last_error} if last_error else None,
    )


async def _recent(session: AsyncSession, site_id: int, limit: int) -> list[Check]:
    return list(
        (
            await session.scalars(
                select(Check).where(Check.site_id == site_id).order_by(Check.checked_at.desc(), Check.id.desc()).limit(limit)
            )
        ).all()
    )


async def reconcile_site(session: AsyncSession, site: Site) -> str | None:
    """Open an issue if an outage has just started, or resolve it if the site has been healthy long enough.
    Returns "opened", "resolved" or None."""
    needed = max(3, settings.uptime_recover_checks)
    checks = await _recent(session, site.id, needed)
    oks = [c.ok for c in checks]

    if outage_started(oks):
        await record_event(session, site, make_event(site, checks[0].error))
        return "opened"

    if recovered(oks, settings.uptime_recover_checks):
        digest = _fingerprint(site, make_event(site, None))
        # the same row lock ingest and the console's actions take
        issue = await session.scalar(
            select(Issue).where(Issue.site_id == site.id, Issue.fingerprint == digest, Issue.status == "open").with_for_update()
        )
        if issue is not None:
            issue.status = "resolved"
            issue.snoozed_until = None
            session.add(IssueAction(issue_id=issue.id, action="resolve", detail=f"auto: {settings.uptime_recover_checks} checks in a row passed"))
            await dismiss_alerts(session, issue.id)
            await session.commit()
            return "resolved"
        await session.rollback()  # release the lock taken above, if any
    return None


async def reconcile(session: AsyncSession, site_ids: list[int]) -> None:
    """Run `reconcile_site` for each probed site. A failure for one site is logged and never stops the
    others, and never reaches the probe: a watchdog must keep watching."""
    for site_id in site_ids:
        try:
            site = await session.get(Site, site_id)
            if site is not None:
                await reconcile_site(session, site)
        except Exception:
            log.exception("could not reconcile uptime for site %s", site_id)
            await session.rollback()
