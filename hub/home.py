import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .edge import auto_resolve, open_new
from .ingest import _load_site, verify_and_note
from .models import HomeReport, Site, SpeedTest
from .schemas import EventIn, Frame, HomeAck, HomeOut, HomeReportIn, SpeedPoint
from .stacks import STALE_AFTER

log = logging.getLogger("nightjar.home")

# The home host (the ThinkCentre) is watched, never controlled: a reporter on it pushes its
# temperature, disk, memory, backup age and internet speed. Same shape and rules as hub/stacks.py
# and hub/media.py: silence is shown as silence, problems open once when they start, and resolve
# themselves after staying gone.
ingest_router = APIRouter(prefix="/ingest", tags=["home"])
router = APIRouter(prefix="/api", tags=["home"])

CPU_HOT_C = 85  # the cabinet's target is under 80 C sustained, so 85 is "look at the airflow"
DISK_LOW_FRACTION = 0.10
BACKUP_STALE = timedelta(hours=36)  # a nightly job that missed one night and is late for the next
SPEED_POINTS = 24

# What becomes an issue: (exception type, fixed message, module, function, level). Two are P2
# (error) and the backup is P3 (warning, digest only). None is P1: a home-host problem may never
# wake the phone. The message is fixed so every occurrence groups into one issue.
PROBLEMS = {
    "cpu_hot": ("HomeHostHot", "the home server is running hot", "home", "cpu", "error"),
    "disk_low": ("HomeDiskLow", "the home server disk is nearly full", "home", "disk", "error"),
    "backup_stale": ("BackupStale", "the nightly backup has not completed", "home", "backup", "warning"),
}


def _conditions(
    cpu_temp: float | None,
    free: int | None,
    total: int | None,
    backup_configured: bool,
    backup_ok_at: datetime | None,
    at: datetime,
) -> set[str]:
    bad = set()
    if cpu_temp is not None and cpu_temp >= CPU_HOT_C:
        bad.add("cpu_hot")
    if total and free is not None and free < DISK_LOW_FRACTION * total:
        bad.add("disk_low")
    # A backup that is being tracked and has never completed is as stale as one that stopped.
    if backup_configured and (backup_ok_at is None or at - backup_ok_at > BACKUP_STALE):
        bad.add("backup_stale")
    return bad


def _event(key: str, payload: HomeReportIn) -> EventIn:
    exception_type, message, module, function, level = PROBLEMS[key]
    return EventIn(
        exception_type=exception_type,
        message=message,
        frames=[Frame(module=module, function=function)],
        level=level,
        environment="home",
        extra={
            "cpu_temp_c": payload.host.cpu_temp_c,
            "disk_free_mb": payload.host.disk_free_mb,
            "disk_total_mb": payload.host.disk_total_mb,
            "backup_ok_at": payload.backup_ok_at.isoformat() if payload.backup_ok_at else None,
        },
    )


def _row_conditions(r: HomeReport) -> set[str]:
    return _conditions(r.cpu_temp_c, r.disk_free_mb, r.disk_total_mb, r.backup_configured, r.backup_ok_at, r.reported_at)


@ingest_router.post("/{ingest_key}/home", response_model=HomeAck, status_code=201)
async def record_home(
    ingest_key: str,
    request: Request,
    x_hub_signature: str | None = Header(default=None),
    x_hub_timestamp: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> HomeAck:
    raw = await request.body()
    site = await _load_site(session, ingest_key)
    await verify_and_note(session, request, site, raw, x_hub_signature, x_hub_timestamp)
    try:
        payload = HomeReportIn.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc

    now = datetime.now(UTC)
    rows = (
        await session.scalars(
            select(HomeReport)
            .where(HomeReport.site_id == site.id, HomeReport.reported_at >= now - timedelta(minutes=25))
            .order_by(HomeReport.reported_at)
        )
    ).all()
    prev = rows[-1] if rows else None
    h = payload.host
    now_bad = _conditions(h.cpu_temp_c, h.disk_free_mb, h.disk_total_mb, payload.backup_configured, payload.backup_ok_at, now)
    was_bad = _row_conditions(prev) if prev else set()

    report = HomeReport(
        site_id=site.id,
        cpu_temp_c=h.cpu_temp_c,
        load_1=h.load_1,
        cpu_count=h.cpu_count,
        mem_total_mb=h.mem_total_mb,
        mem_available_mb=h.mem_available_mb,
        disk_total_mb=h.disk_total_mb,
        disk_free_mb=h.disk_free_mb,
        uptime_s=h.uptime_s,
        intake_c=h.intake_c,
        exhaust_c=h.exhaust_c,
        rx_bps=h.rx_bps,
        tx_bps=h.tx_bps,
        backup_configured=payload.backup_configured,
        backup_ok_at=payload.backup_ok_at,
    )
    session.add(report)
    if payload.net is not None:
        # The reporter repeats its last speed test on every report until it runs the next one, so
        # the same test arrives many times. The unique (site, tested_at) index keeps the first.
        await session.execute(
            insert(SpeedTest)
            .values(
                site_id=site.id,
                tested_at=payload.net.tested_at,
                down_mbps=payload.net.down_mbps,
                up_mbps=payload.net.up_mbps,
                latency_ms=payload.net.latency_ms,
            )
            .on_conflict_do_nothing(index_elements=["site_id", "tested_at"])
        )
    await session.commit()
    report_id = report.id  # read now: a rollback below expires every object

    await open_new(session, site, now_bad - was_bad, lambda key: _event(key, payload))
    try:
        timeline = [(r.reported_at, _row_conditions(r)) for r in rows] + [(now, now_bad)]
        await auto_resolve(
            session, site, PROBLEMS, lambda key: _event(key, payload), timeline, now, now_bad,
            "the home server has been healthy",
        )
    except Exception:
        log.exception("could not auto-resolve home problems")
        await session.rollback()
    return HomeAck(id=report_id)


@router.get("/home", response_model=list[HomeOut])
async def list_home(session: AsyncSession = Depends(get_session)) -> list[HomeOut]:
    now = datetime.now(UTC)
    # Newest report per home site, however old, so a dead reporter is still listed (as stale).
    latest = (
        await session.scalars(
            select(HomeReport)
            .distinct(HomeReport.site_id)
            .order_by(HomeReport.site_id, HomeReport.reported_at.desc())
        )
    ).all()
    out = []
    for r in latest:
        site = await session.scalar(select(Site.name).where(Site.id == r.site_id))
        tests = (
            await session.scalars(
                select(SpeedTest)
                .where(SpeedTest.site_id == r.site_id)
                .order_by(SpeedTest.tested_at.desc())
                .limit(SPEED_POINTS)
            )
        ).all()
        newest = tests[0] if tests else None
        out.append(
            HomeOut(
                site=site,
                stale=now - r.reported_at > STALE_AFTER,
                reported_at=r.reported_at,
                cpu_temp_c=r.cpu_temp_c,
                load_1=r.load_1,
                cpu_count=r.cpu_count,
                mem_total_mb=r.mem_total_mb,
                mem_available_mb=r.mem_available_mb,
                disk_total_mb=r.disk_total_mb,
                disk_free_mb=r.disk_free_mb,
                uptime_s=r.uptime_s,
                intake_c=r.intake_c,
                exhaust_c=r.exhaust_c,
                rx_bps=r.rx_bps,
                tx_bps=r.tx_bps,
                backup_configured=r.backup_configured,
                backup_ok_at=r.backup_ok_at,
                backup_age_h=round((now - r.backup_ok_at).total_seconds() / 3600, 1) if r.backup_ok_at else None,
                net=SpeedPoint(
                    tested_at=newest.tested_at,
                    down_mbps=newest.down_mbps,
                    up_mbps=newest.up_mbps,
                    latency_ms=newest.latency_ms,
                )
                if newest
                else None,
                speed_series=[t.down_mbps for t in reversed(tests)],
            )
        )
    out.sort(key=lambda o: o.site)
    return out
