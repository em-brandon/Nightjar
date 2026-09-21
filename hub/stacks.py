import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .ingest import _load_site, _verify, record_event
from .models import Site, StackReport
from .schemas import EventIn, Frame, ServiceIn, StackAck, StackOut, StackReportIn

log = logging.getLogger("nightjar.stacks")

# A reporter on the staging host pushes in; the console reads. The hub never
# reaches out to that machine, and is never given access to its Docker.
ingest_router = APIRouter(prefix="/ingest", tags=["stacks"])
router = APIRouter(prefix="/api", tags=["stacks"])

# The reporter runs every minute. Three missed reports and the numbers are
# treated as stale: a reporter that has died looks exactly like a stack that
# is fine, so silence has to be visible.
STALE_AFTER = timedelta(seconds=180)
SERIES_POINTS = 30
SERIES_WINDOW = timedelta(hours=3)
# Three restarts inside ten minutes is a loop, not a deploy.
RESTART_WINDOW = timedelta(minutes=10)
RESTART_LOOP_MIN = 3
MEM_HIGH_FRACTION = 0.85
DISK_LOW_FRACTION = 0.10

# What becomes an issue. error is P2 and warning is P3 (digest only), so memory
# pressure never wakes anyone. The message is fixed text: the fingerprint
# groups every occurrence into one issue, and the numbers ride in `extra`.
PROBLEMS = {
    "degraded": ("StackDegraded", "stack {stack} is degraded", "error"),
    "restart_loop": ("StackRestartLoop", "stack {stack} is restarting repeatedly", "error"),
    "memory_high": ("StackMemoryHigh", "stack {stack} is near its memory limit", "warning"),
    "disk_low": ("HostDiskLow", "staging host disk is nearly full", "error"),
}


def _restart_delta(rows: list, at: datetime, restarts: int | None) -> int:
    """Restarts in the RESTART_WINDOW ending at `at`, against the earliest earlier
    report inside it. With nothing to compare against it is 0, never a guess. A
    recreated container resets its counter, which is not negative restarts."""
    if restarts is None:
        return 0
    for r in rows:
        if r.reported_at >= at - RESTART_WINDOW and r.restarts is not None:
            return max(0, restarts - r.restarts)
    return 0


def _conditions(state, mem, limit, free, total, delta) -> set[str]:
    bad = set()
    if state == "degraded":
        bad.add("degraded")
    if delta >= RESTART_LOOP_MIN:
        bad.add("restart_loop")
    if limit and mem is not None and mem >= MEM_HIGH_FRACTION * limit:
        bad.add("memory_high")
    if total and free is not None and free < DISK_LOW_FRACTION * total:
        bad.add("disk_low")
    return bad


def _event(key: str, payload: StackReportIn) -> EventIn:
    exception_type, template, level = PROBLEMS[key]
    # Disk is a property of the host, so every stack on it shares one issue.
    module, function = ("host", "disk") if key == "disk_low" else ("stack", payload.stack)
    return EventIn(
        exception_type=exception_type,
        message=template.format(stack=payload.stack),
        frames=[Frame(module=module, function=function)],
        level=level,
        environment="staging",
        extra={
            "stack": payload.stack,
            "state": payload.state,
            "mem_mb": payload.mem_mb,
            "mem_limit_mb": payload.mem_limit_mb,
            "host_disk_free_mb": payload.host_disk_free_mb,
            "host_disk_total_mb": payload.host_disk_total_mb,
        },
    )


@ingest_router.post("/{ingest_key}/stack", response_model=StackAck, status_code=201)
async def record_stack(
    ingest_key: str,
    request: Request,
    x_hub_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> StackAck:
    raw = await request.body()
    site = await _load_site(session, ingest_key)
    _verify(site.ingest_secret, raw, x_hub_signature)
    try:
        payload = StackReportIn.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc

    now = datetime.now(UTC)
    # Recent history for this stack, read BEFORE the new report goes in, so we can
    # tell what has just *changed*.
    rows = (
        await session.scalars(
            select(StackReport)
            .where(
                StackReport.site_id == site.id,
                StackReport.stack == payload.stack,
                StackReport.reported_at >= now - timedelta(minutes=25),
            )
            .order_by(StackReport.reported_at)
        )
    ).all()
    prev = rows[-1] if rows else None

    now_bad = _conditions(
        payload.state, payload.mem_mb, payload.mem_limit_mb,
        payload.host_disk_free_mb, payload.host_disk_total_mb,
        _restart_delta(rows, now, payload.restarts),
    )
    was_bad = (
        _conditions(
            prev.state, prev.mem_mb, prev.mem_limit_mb,
            prev.host_disk_free_mb, prev.host_disk_total_mb,
            _restart_delta(rows[:-1], prev.reported_at, prev.restarts),
        )
        if prev
        else set()
    )

    report = StackReport(
        site_id=site.id,
        stack=payload.stack,
        state=payload.state,
        mem_mb=payload.mem_mb,
        mem_limit_mb=payload.mem_limit_mb,
        cpu_pct=payload.cpu_pct,
        host_mem_mb=payload.host_mem_mb,
        disk_mb=payload.disk_mb,
        restarts=payload.restarts,
        host_disk_total_mb=payload.host_disk_total_mb,
        host_disk_free_mb=payload.host_disk_free_mb,
        services=[s.model_dump() for s in payload.services],
    )
    session.add(report)
    await session.commit()

    # Edge-triggered: only a problem that has just STARTED opens an event. One per
    # report while a stack stays degraded would be 1,440 a day and would trip the
    # spike detector. Repeats of a known issue are deduplicated by the pipeline.
    for key in sorted(now_bad - was_bad):
        try:
            await record_event(session, site, _event(key, payload))
        except Exception:
            # The report is already saved. A failure to open an issue must not
            # turn into a failed report, and must not poison the session.
            log.exception("could not record %s for %s", key, payload.stack)
            await session.rollback()
    return StackAck(id=report.id)


@router.get("/stacks", response_model=list[StackOut])
async def list_stacks(session: AsyncSession = Depends(get_session)) -> list[StackOut]:
    now = datetime.now(UTC)

    # Newest report per (site, stack), however old, so a dead reporter is still
    # listed (as stale) instead of vanishing. Keyed on the stack as well as the
    # site: two projects sharing one site key must not overwrite each other.
    latest = (
        await session.scalars(
            select(StackReport)
            .distinct(StackReport.site_id, StackReport.stack)
            .order_by(StackReport.site_id, StackReport.stack, StackReport.reported_at.desc())
        )
    ).all()
    if not latest:
        return []

    sites = {
        s.id: s
        for s in await session.scalars(select(Site).where(Site.id.in_([r.site_id for r in latest])))
    }

    series: dict[tuple[int, str], list[int | None]] = defaultdict(list)
    # earliest restart count inside the window: the baseline for the delta
    baseline: dict[tuple[int, str], int | None] = {}
    for site_id, stack, mem, restarts, at in await session.execute(
        select(
            StackReport.site_id,
            StackReport.stack,
            StackReport.mem_mb,
            StackReport.restarts,
            StackReport.reported_at,
        )
        .where(StackReport.reported_at >= now - SERIES_WINDOW)
        .order_by(StackReport.site_id, StackReport.stack, StackReport.reported_at)
    ):
        key = (site_id, stack)
        series[key].append(mem)
        if at >= now - RESTART_WINDOW and key not in baseline:
            baseline[key] = restarts

    def restarts_10m(r: StackReport) -> int | None:
        start = baseline.get((r.site_id, r.stack))
        if start is None or r.restarts is None:
            return None
        # a recreated container resets its counter; that is not negative restarts
        return max(0, r.restarts - start)

    out = [
        StackOut(
            site=sites[r.site_id].name,
            env=sites[r.site_id].env,
            stack=r.stack,
            state=r.state,
            stale=now - r.reported_at > STALE_AFTER,
            reported_at=r.reported_at,
            mem_mb=r.mem_mb,
            mem_limit_mb=r.mem_limit_mb,
            cpu_pct=r.cpu_pct,
            host_mem_mb=r.host_mem_mb,
            disk_mb=r.disk_mb,
            host_disk_total_mb=r.host_disk_total_mb,
            host_disk_free_mb=r.host_disk_free_mb,
            restarts=r.restarts,
            restarts_10m=restarts_10m(r),
            restart_loop=(restarts_10m(r) or 0) >= RESTART_LOOP_MIN and not now - r.reported_at > STALE_AFTER,
            services=[ServiceIn(**svc) for svc in r.services],
            mem_series=series[(r.site_id, r.stack)][-SERIES_POINTS:],
        )
        for r in latest
    ]
    out.sort(key=lambda o: (o.site, o.stack))
    return out
