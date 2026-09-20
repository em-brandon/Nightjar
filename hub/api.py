from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .models import Check, Issue, Site
from .schemas import DayState, IssueOut, SiteHealthOut

# Read-only, for the console. Everything the console shows is derived from
# issues and checks; there is no separate health table to drift out of sync.
router = APIRouter(prefix="/api", tags=["console"])

ISSUE_LIMIT = 100
RIBBON_DAYS = 90
SERIES_POINTS = 30
# A day with this share of failed checks or more is an outage; any failure
# below that is a degraded day. Same 99.7% can be one long outage or forty
# blips, and the ribbon exists to show which.
OUTAGE_RATIO = 0.10
# Two failed checks in a row before a site is "down". One is a blip, and a
# probe that cries wolf on every dropped packet gets ignored like any other.
DOWN_STREAK = 2


@router.get("/issues", response_model=list[IssueOut])
async def list_issues(session: AsyncSession = Depends(get_session)) -> list[IssueOut]:
    # P1 before P2 before P3, then most recently active. "P1" < "P2" < "P3"
    # sorts correctly as text, but an explicit case survives a new severity.
    rank = case((Issue.severity == "P1", 1), (Issue.severity == "P2", 2), else_=3)
    rows = await session.execute(
        select(Issue, Site.name, Site.env)
        .join(Site, Issue.site_id == Site.id)
        .where(Issue.status == "open")
        .order_by(rank, Issue.last_seen.desc())
        .limit(ISSUE_LIMIT)
    )
    return [
        IssueOut(
            id=issue.id,
            site=site_name,
            env=env,
            severity=issue.severity,
            title=issue.title,
            culprit=issue.culprit,
            fingerprint=issue.fingerprint,
            event_count=issue.event_count,
            first_seen=issue.first_seen,
            last_seen=issue.last_seen,
        )
        for issue, site_name, env in rows
    ]


def _ribbon(days: dict, today) -> list[DayState]:
    """90 segments, oldest first. `gap` is a day with no checks at all, so
    'no data' can never be mistaken for 'all good'."""
    out: list[DayState] = []
    for back in range(RIBBON_DAYS - 1, -1, -1):
        total, failed = days.get(today - timedelta(days=back), (0, 0))
        if total == 0:
            out.append("gap")
        elif failed == 0:
            out.append("ok")
        elif failed / total >= OUTAGE_RATIO:
            out.append("out")
        else:
            out.append("deg")
    return out


@router.get("/sites/health", response_model=list[SiteHealthOut])
async def sites_health(session: AsyncSession = Depends(get_session)) -> list[SiteHealthOut]:
    is_open = Issue.status == "open"

    def open_count(severity: str):
        return func.count(Issue.id).filter(is_open, Issue.severity == severity)

    # Outer join so a site with no issues at all still appears, as "up".
    rows = (
        await session.execute(
            select(
                Site.id,
                Site.name,
                Site.env,
                Site.url,
                open_count("P1"),
                open_count("P2"),
                open_count("P3"),
                func.max(Issue.last_seen),
            )
            .select_from(Site)
            .outerjoin(Issue, Issue.site_id == Site.id)
            .group_by(Site.id)
            .order_by(Site.name)
        )
    ).all()

    now = datetime.now(UTC)
    today = now.date()

    # Last 24h of raw checks, per site, oldest first.
    recent: dict[int, list] = defaultdict(list)
    for sid, ok, ms, at in await session.execute(
        select(Check.site_id, Check.ok, Check.latency_ms, Check.checked_at)
        .where(Check.checked_at >= now - timedelta(hours=24))
        .order_by(Check.site_id, Check.checked_at)
    ):
        recent[sid].append((ok, ms, at))

    # Newest check per site, however old. Kept separate from `recent` so a
    # prober that stopped 3 hours ago is still visible as stale.
    latest = {
        c.site_id: c.checked_at
        for c in await session.scalars(
            select(Check).distinct(Check.site_id).order_by(Check.site_id, Check.checked_at.desc())
        )
    }

    # One row per (site, UTC day) for the ribbon.
    day = func.date_trunc("day", Check.checked_at)
    daily: dict[int, dict] = defaultdict(dict)
    for sid, d, total, failed in await session.execute(
        select(Check.site_id, day, func.count(), func.count().filter(Check.ok.is_(False)))
        .where(Check.checked_at >= now - timedelta(days=RIBBON_DAYS))
        .group_by(Check.site_id, day)
    ):
        daily[sid][d.date()] = (total, failed)

    stale_after = timedelta(seconds=max(300, 3 * settings.probe_interval_s))
    out = []
    for site_id, name, env, url, p1, p2, p3, last_event in rows:
        checks = recent.get(site_id, [])
        monitored = url is not None
        last_check = latest.get(site_id)

        # A probe that has gone quiet is not evidence of health.
        stale = monitored and (last_check is None or now - last_check > stale_after)

        streak = 0
        for ok, _, _ in reversed(checks):
            if ok:
                break
            streak += 1

        state = "up"
        if p2 or (monitored and streak >= 1) or stale:
            state = "warn"
        if p1 or (monitored and streak >= DOWN_STREAK):
            state = "down"

        successes = [ms for ok, ms, _ in checks if ok and ms is not None]
        out.append(
            SiteHealthOut(
                id=site_id,
                name=name,
                env=env,
                state=state,
                open_p1=p1,
                open_p2=p2,
                open_p3=p3,
                last_event=last_event,
                monitored=monitored,
                probe_stale=bool(stale),
                last_check=last_check,
                uptime_24h=(
                    round(100 * sum(1 for c in checks if c[0]) / len(checks), 2)
                    if checks
                    else None
                ),
                latency_ms=checks[-1][1] if checks and checks[-1][0] else None,
                latency_avg_ms=round(sum(successes) / len(successes)) if successes else None,
                latency_series=[ms if ok else None for ok, ms, _ in checks[-SERIES_POINTS:]],
                ribbon=_ribbon(daily.get(site_id, {}), today) if monitored else [],
            )
        )
    return out
