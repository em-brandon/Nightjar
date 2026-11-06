"""Availability reports: how each client's sites did over a month. Read-only (docs/REPORTS.md).

Built from data Nightjar already owns: `daily_stats` for days it has rolled up, the raw `checks` for the days it has
not yet, so a report is never a day behind and still works after the raw rows are pruned. Nothing here is written, and
nothing is sent: this is what a client report WOULD say, shown to the operator first.

Rules that keep the numbers honest:

  * **Silence is not uptime.** A site with no checks has no percentage, not 100%. Missing probe rounds are reported as
    lower `coverage_pct`, so a report built over a gap says so instead of looking perfect.
  * **No percentage rounds up into a lie.** 99.996% is shown as 99.99%, never 100.00%: if a check failed, the report
    cannot say "perfect".
  * **Production only.** Staging is not something a client is promised.
  * **Counts and times, never internals** (docs/REPORTS.md, "Client-facing privacy"). No issue titles, no probe error
    text, no full URL (a host only), nothing that came from an exception.
  * **Months are UTC**, the same days `daily_stats` is cut on.
"""

import math
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .models import Client, DailyStat, Deploy, Site
from .probe import RETENTION
from .rollup import _CHECKS as _RAW_DAY_CHECKS

router = APIRouter(prefix="/api/reports", tags=["reports"])

MAX_MONTHS_BACK = 36
# Below this share of the expected checks having run, the report says monitoring was incomplete.
COVERAGE_WARN_PCT = 90.0

Verdict = Literal["no_data", "perfect", "met", "below"]


# --------------------------------------------------------------------------------------------- pure helpers
def month_bounds(month: str | None, now: datetime) -> tuple[datetime, datetime, bool]:
    """(start, end exclusive, partial) for 'YYYY-MM' (default: this month). Raises ValueError with a reason."""
    now = now.astimezone(UTC)
    if month is None:
        year, mon = now.year, now.month
    else:
        match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", month)
        if not match:
            raise ValueError("month must look like 2026-10")
        year, mon = int(match[1]), int(match[2])
    start = datetime(year, mon, 1, tzinfo=UTC)
    if start > now:
        raise ValueError("that month has not started yet")
    earliest_index = now.year * 12 + now.month - 1 - MAX_MONTHS_BACK
    if year * 12 + mon - 1 < earliest_index:
        raise ValueError(f"reports reach back {MAX_MONTHS_BACK} months")
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return start, end, end > now


def uptime_pct(ok: int, total: int) -> float | None:
    """None without checks. Never 100.0 unless every check passed: rounding must not hide a failure."""
    if total <= 0:
        return None
    if ok >= total:
        return 100.0
    return min(99.99, math.floor(10_000 * ok / total) / 100)


def downtime_minutes(failed_checks: int, interval_s: int) -> int:
    """Each failed check stands for one probe interval. An estimate, and worded as one in the report."""
    return round(failed_checks * interval_s / 60)


def coverage_pct(total: int, first_day: date | None, through: datetime, interval_s: int) -> float | None:
    """How much of the time the probe was actually checking, from the first day it has data to now (or month end)."""
    if first_day is None or interval_s <= 0:
        return None
    span = (through - datetime.combine(first_day, time.min, tzinfo=UTC)).total_seconds()
    expected = span / interval_s
    return None if expected <= 0 else min(100.0, round(100 * total / expected, 1))


def headline(verdicts: list[tuple[str, float | None]], target: float, incomplete: bool) -> tuple[Verdict, str]:
    """The sentence at the top of a client's report. `verdicts` is (site name, uptime %) for each monitored site."""
    measured = [(n, p) for n, p in verdicts if p is not None]
    if not measured:
        return "no_data", "There is no availability data for this period."
    below = [(n, p) for n, p in measured if p < target]
    note = " Monitoring was incomplete for part of this period." if incomplete else ""
    if below:
        who = "; ".join(f"{n} ({p:g}%)" for n, p in below)
        return "below", f"Availability was below the {target:g}% target for: {who}.{note}"
    sites = "The site was" if len(measured) == 1 else "All sites were"
    if all(p == 100.0 for _, p in measured):
        return "perfect", f"{sites} available for the whole period.{note}"
    return "met", f"{sites} available, meeting the {target:g}% target.{note}"


# --------------------------------------------------------------------------------------------- response shapes
class IncidentOut(BaseModel):
    started_at: datetime
    # None while the outage is still going
    ended_at: datetime | None
    minutes: int
    ongoing: bool


class SiteReport(BaseModel):
    site_id: int
    name: str
    # the host only: a URL can carry a token or a query string
    host: str
    uptime_pct: float | None
    downtime_minutes: int
    checks_total: int
    checks_ok: int
    coverage_pct: float | None
    latency_avg_ms: int | None
    # the slowest day's 95th percentile (percentiles of days cannot be averaged into a month's)
    latency_peak_p95_ms: int | None
    met_target: bool | None
    incidents: list[IncidentOut]
    # False when the month is older than the raw checks are kept: the totals are exact, the outage list is not
    incident_detail: bool
    deploys: int
    failed_deploys: int


class ClientReport(BaseModel):
    client_id: int
    name: str
    contact_email: str | None
    verdict: Verdict
    headline: str
    uptime_pct: float | None
    sites: list[SiteReport]
    # production sites with no url: they report crashes but nothing says whether they are up, so no number is made up
    not_monitored: list[str]


class AvailabilityOut(BaseModel):
    month: str
    period_start: datetime
    period_end: datetime
    # the month is not over: figures are to date
    partial: bool
    through: datetime
    target_pct: float
    probe_interval_s: int
    overall_uptime_pct: float | None
    clients: list[ClientReport]


# --------------------------------------------------------------------------------------------- queries
# Runs of failed checks, two or more in a row (the same rule that opens an outage, hub/uptime.py): one failure is a blip
# and counts toward the totals but is not an incident. A run also ends where the checks stop for a while (:gap seconds):
# two failures with the probe off between them are two sightings, not one outage that lasted the whole silence.
# Gaps and islands: a running count of "a new run started here" labels each run.
_OUTAGES = text(
    """
    WITH c AS (
        SELECT site_id, checked_at, ok,
               lag(ok) OVER w AS prev_ok, lag(checked_at) OVER w AS prev_at
        FROM checks
        WHERE site_id = ANY(:ids) AND checked_at >= :start AND checked_at < :end
        WINDOW w AS (PARTITION BY site_id ORDER BY checked_at)
    ), g AS (
        SELECT site_id, checked_at, ok,
               sum(CASE WHEN prev_at IS NULL OR prev_ok IS DISTINCT FROM ok
                          OR checked_at - prev_at > make_interval(secs => CAST(:gap AS double precision))
                        THEN 1 ELSE 0 END) OVER (PARTITION BY site_id ORDER BY checked_at) AS run
        FROM c
    )
    SELECT site_id, min(checked_at) AS first_fail, max(checked_at) AS last_fail, count(*) AS checks
    FROM g
    WHERE NOT ok
    GROUP BY site_id, run
    HAVING count(*) >= 2
    ORDER BY site_id, first_fail
    """
)
_LAST_CHECK = text("SELECT site_id, max(checked_at) AS last_at FROM checks WHERE site_id = ANY(:ids) GROUP BY site_id")


async def _days(session: AsyncSession, ids: list[int], start: datetime, end: datetime) -> dict[int, dict[date, dict]]:
    """One row per site per day. A rolled-up day wins; a day not rolled up yet is counted from the raw checks."""
    per_site: dict[int, dict[date, dict]] = {i: {} for i in ids}
    raw = await session.execute(_RAW_DAY_CHECKS, {"start": start, "end": end})
    for r in raw.mappings():
        if r["site_id"] in per_site:
            per_site[r["site_id"]][r["day"]] = dict(r)
    rolled = await session.execute(
        select(DailyStat).where(DailyStat.site_id.in_(ids), DailyStat.day >= start.date(), DailyStat.day < end.date())
    )
    for d in rolled.scalars():
        per_site[d.site_id][d.day] = {
            "checks_total": d.checks_total,
            "checks_ok": d.checks_ok,
            "latency_avg_ms": d.latency_avg_ms,
            "latency_p95_ms": d.latency_p95_ms,
        }
    return per_site


def _site_report(
    site: Site,
    days: dict[date, dict],
    outages: list[dict],
    last_check_at: datetime | None,
    deploys: dict[str, int],
    through: datetime,
    now: datetime,
    detail: bool,
) -> SiteReport:
    interval = max(1, settings.probe_interval_s)
    total = sum(d["checks_total"] for d in days.values())
    ok = sum(d["checks_ok"] for d in days.values())
    # average latency over successful checks, weighted by how many each day had
    weighted = [(d["latency_avg_ms"], d["checks_ok"]) for d in days.values() if d["latency_avg_ms"] is not None and d["checks_ok"]]
    avg = round(sum(a * n for a, n in weighted) / sum(n for _, n in weighted)) if weighted else None
    peaks = [d["latency_p95_ms"] for d in days.values() if d["latency_p95_ms"] is not None]
    pct = uptime_pct(ok, total)
    # still going: the newest check anywhere is one of the failures in this run, and it is recent
    fresh = timedelta(seconds=max(300, 3 * interval))
    incidents = []
    for o in outages:
        ongoing = last_check_at is not None and o["last_fail"] >= last_check_at and now - o["last_fail"] <= fresh
        ended = None if ongoing else o["last_fail"] + timedelta(seconds=interval)
        minutes = max(1, round(((o["last_fail"] - o["first_fail"]).total_seconds() + interval) / 60))
        incidents.append(IncidentOut(started_at=o["first_fail"], ended_at=ended, minutes=minutes, ongoing=ongoing))
    return SiteReport(
        site_id=site.id,
        name=site.name,
        host=urlsplit(site.url or "").hostname or "",
        uptime_pct=pct,
        downtime_minutes=downtime_minutes(total - ok, interval),
        checks_total=total,
        checks_ok=ok,
        coverage_pct=coverage_pct(total, min(days) if days else None, through, interval),
        latency_avg_ms=avg,
        latency_peak_p95_ms=max(peaks) if peaks else None,
        met_target=None if pct is None else pct >= settings.report_uptime_target,
        incidents=incidents,
        incident_detail=detail,
        deploys=deploys.get("success", 0),
        failed_deploys=deploys.get("failed", 0),
    )


@router.get("/availability", response_model=AvailabilityOut)
async def availability(
    month: str | None = Query(default=None, description="YYYY-MM, UTC. Default: this month so far"),
    session: AsyncSession = Depends(get_session),
) -> AvailabilityOut:
    now = datetime.now(UTC)
    try:
        start, end, partial = month_bounds(month, now)
    except ValueError as err:
        raise HTTPException(422, str(err)) from err
    through = min(end, now)

    sites = list(
        (
            await session.scalars(
                select(Site).join(Client).where(Site.env == "production").order_by(Client.name, Site.name)
            )
        ).all()
    )
    monitored = [s for s in sites if s.url]
    ids = [s.id for s in monitored]

    days = await _days(session, ids, start, end) if ids else {}
    outages: dict[int, list[dict]] = {i: [] for i in ids}
    last_check: dict[int, datetime] = {}
    detail = start >= now - RETENTION
    if ids:
        if detail:
            for r in (await session.execute(_OUTAGES, {"ids": ids, "start": start, "end": end, "gap": max(180, 3 * settings.probe_interval_s)})).mappings():
                outages[r["site_id"]].append(dict(r))
        for r in (await session.execute(_LAST_CHECK, {"ids": ids})).mappings():
            last_check[r["site_id"]] = r["last_at"]
    deploys: dict[int, dict[str, int]] = {i: {} for i in ids}
    if ids:
        for sid, status, n in await session.execute(
            select(Deploy.site_id, Deploy.status, func.count())
            .where(Deploy.site_id.in_(ids), Deploy.created_at >= start, Deploy.created_at < end)
            .group_by(Deploy.site_id, Deploy.status)
        ):
            deploys[sid][status] = n

    by_client: dict[int, list[Site]] = {}
    for s in sites:
        by_client.setdefault(s.client_id, []).append(s)
    names = {c.id: c for c in (await session.scalars(select(Client).where(Client.id.in_(by_client)))).all()}

    clients: list[ClientReport] = []
    all_ok = all_total = 0
    for cid, group in sorted(by_client.items(), key=lambda kv: names[kv[0]].name.lower()):
        reports = [
            _site_report(s, days.get(s.id, {}), outages.get(s.id, []), last_check.get(s.id), deploys.get(s.id, {}), through, now, detail)
            for s in group
            if s.url
        ]
        c_total = sum(r.checks_total for r in reports)
        c_ok = sum(r.checks_ok for r in reports)
        all_total += c_total
        all_ok += c_ok
        incomplete = any(r.coverage_pct is not None and r.coverage_pct < COVERAGE_WARN_PCT for r in reports)
        verdict, text_ = headline([(r.name, r.uptime_pct) for r in reports], settings.report_uptime_target, incomplete)
        clients.append(
            ClientReport(
                client_id=cid,
                name=names[cid].name,
                contact_email=names[cid].contact_email,
                verdict=verdict,
                headline=text_,
                uptime_pct=uptime_pct(c_ok, c_total),
                sites=reports,
                not_monitored=[s.name for s in group if not s.url],
            )
        )
    return AvailabilityOut(
        month=start.strftime("%Y-%m"),
        period_start=start,
        period_end=end,
        partial=partial,
        through=through,
        target_pct=settings.report_uptime_target,
        probe_interval_s=max(1, settings.probe_interval_s),
        overall_uptime_pct=uptime_pct(all_ok, all_total),
        clients=clients,
    )
