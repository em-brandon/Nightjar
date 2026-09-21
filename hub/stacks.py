from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .ingest import _load_site, _verify
from .models import Site, StackReport
from .schemas import ServiceIn, StackAck, StackOut, StackReportIn

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

    report = StackReport(
        site_id=site.id,
        stack=payload.stack,
        state=payload.state,
        mem_mb=payload.mem_mb,
        mem_limit_mb=payload.mem_limit_mb,
        cpu_pct=payload.cpu_pct,
        host_mem_mb=payload.host_mem_mb,
        services=[s.model_dump() for s in payload.services],
    )
    session.add(report)
    await session.commit()
    return StackAck(id=report.id)


@router.get("/stacks", response_model=list[StackOut])
async def list_stacks(session: AsyncSession = Depends(get_session)) -> list[StackOut]:
    now = datetime.now(UTC)

    # Newest report per site, however old, so a dead reporter is still listed
    # (as stale) instead of vanishing from the page.
    latest = (
        await session.scalars(
            select(StackReport)
            .distinct(StackReport.site_id)
            .order_by(StackReport.site_id, StackReport.reported_at.desc())
        )
    ).all()
    if not latest:
        return []

    sites = {
        s.id: s
        for s in await session.scalars(select(Site).where(Site.id.in_([r.site_id for r in latest])))
    }

    series: dict[int, list[int | None]] = defaultdict(list)
    for site_id, mem in await session.execute(
        select(StackReport.site_id, StackReport.mem_mb)
        .where(StackReport.reported_at >= now - SERIES_WINDOW)
        .order_by(StackReport.site_id, StackReport.reported_at)
    ):
        series[site_id].append(mem)

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
            services=[ServiceIn(**svc) for svc in r.services],
            mem_series=series[r.site_id][-SERIES_POINTS:],
        )
        for r in latest
    ]
    out.sort(key=lambda o: o.site)
    return out
