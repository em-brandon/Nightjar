from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .models import Check, Client, Event, Issue, IssueAction, Site
from .schemas import DayState, IssueOut, RisingOut, SiteHealthOut

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


async def acknowledged_ids(session: AsyncSession, issue_ids: list[int]) -> set[int]:
    """Issues someone has looked at since they last opened. An ack counts until
    a later resolve or reopen, so a regression is not born already-acknowledged."""
    if not issue_ids:
        return set()
    acked: set[int] = set()
    for issue_id, action in await session.execute(
        select(IssueAction.issue_id, IssueAction.action)
        .where(IssueAction.issue_id.in_(issue_ids))
        .order_by(IssueAction.created_at, IssueAction.id)
    ):
        if action == "ack":
            acked.add(issue_id)
        elif action in ("resolve", "reopen"):
            acked.discard(issue_id)
    return acked


def issue_out(issue: Issue, site_name: str, env: str, acked: bool) -> IssueOut:
    return IssueOut(
        id=issue.id,
        site=site_name,
        env=env,
        severity=issue.severity,
        status=issue.status,
        snoozed_until=issue.snoozed_until,
        acknowledged=acked,
        title=issue.title,
        culprit=issue.culprit,
        fingerprint=issue.fingerprint,
        event_count=issue.event_count,
        first_seen=issue.first_seen,
        last_seen=issue.last_seen,
    )


@router.get("/issues", response_model=list[IssueOut])
async def list_issues(
    status: Literal["open", "all"] = "open",
    session: AsyncSession = Depends(get_session),
) -> list[IssueOut]:
    # Open first, then P1 before P2 before P3, then most recently active. An
    # explicit case survives a new severity where text ordering would not.
    rank = case((Issue.severity == "P1", 1), (Issue.severity == "P2", 2), else_=3)
    query = (
        select(Issue, Site.name, Site.env)
        .join(Site, Issue.site_id == Site.id)
        .order_by(case((Issue.status == "open", 0), else_=1), rank, Issue.last_seen.desc())
        .limit(ISSUE_LIMIT)
    )
    if status == "open":
        query = query.where(Issue.status == "open")
    rows = (await session.execute(query)).all()
    acked = await acknowledged_ids(session, [issue.id for issue, _, _ in rows])
    return [issue_out(issue, name, env, issue.id in acked) for issue, name, env in rows]


RISE_MIN = 5  # fewer than this in 30 minutes is noise, not a trend
BUCKET_MIN = 10


@router.get("/errors/rising", response_model=list[RisingOut])
async def errors_rising(session: AsyncSession = Depends(get_session)) -> list[RisingOut]:
    """Open issues whose event rate over the last 30 minutes is more than double
    the 30 minutes before it. This is a look-at-it list, not an alert: the
    spike alert stays in ingest, where it belongs."""
    now = datetime.now(UTC)
    since = now - timedelta(minutes=6 * BUCKET_MIN)
    buckets: dict[int, list[int]] = defaultdict(lambda: [0] * 6)
    for issue_id, at in await session.execute(
        select(Event.issue_id, Event.occurred_at)
        .join(Issue, Event.issue_id == Issue.id)
        .where(Issue.status == "open", Event.occurred_at >= since)
    ):
        idx = min(5, int((at - since).total_seconds() // (BUCKET_MIN * 60)))
        buckets[issue_id][idx] += 1

    rising = []
    for issue_id, series in buckets.items():
        previous, recent = sum(series[:3]), sum(series[3:])
        if recent >= RISE_MIN and recent > 2 * previous:
            rising.append((issue_id, series, previous, recent))
    if not rising:
        return []

    info = {
        issue.id: (issue, name)
        for issue, name in await session.execute(
            select(Issue, Site.name)
            .join(Site, Issue.site_id == Site.id)
            .where(Issue.id.in_([r[0] for r in rising]))
        )
    }
    rising.sort(key=lambda r: r[3], reverse=True)
    return [
        RisingOut(
            issue_id=iid,
            site=info[iid][1],
            title=info[iid][0].title,
            severity=info[iid][0].severity,
            recent=recent,
            previous=previous,
            series=series,
        )
        for iid, series, previous, recent in rising[:10]
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
                Client.name,
                open_count("P1"),
                open_count("P2"),
                open_count("P3"),
                func.max(Issue.last_seen),
            )
            .select_from(Site)
            .join(Client, Client.id == Site.client_id)
            .outerjoin(Issue, Issue.site_id == Site.id)
            # The home media server has its own tab. As a "site" it would appear
            # among the client sites, on the verdict and the wall display.
            .where(Site.env.not_in(["media", "home"]))
            .group_by(Site.id, Client.name)
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
    for site_id, name, env, url, client, p1, p2, p3, last_event in rows:
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
                client=client,
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
