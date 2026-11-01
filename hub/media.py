import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .edge import auto_resolve, open_new
from .ingest import _load_site, _verify
from .derive import site_secret
from .models import MediaPlay, MediaReport, Site
from .schemas import (
    EventIn,
    Frame,
    MediaAck,
    MediaOut,
    MediaPlayOut,
    MediaReportIn,
    MediaSessionIn,
)
from .stacks import STALE_AFTER

log = logging.getLogger("nightjar.media")

# The home media server (Jellyfin) is watched, never controlled. A reporter on its
# host pushes in, and the hub never reaches into the home network. Same shape as
# hub/stacks.py, and it borrows that file's rules: silence is shown as silence,
# problems are edge-triggered, and they resolve themselves once they stay gone.
ingest_router = APIRouter(prefix="/ingest", tags=["media"])
router = APIRouter(prefix="/api", tags=["media"])

# The same session on the same item, seen again within this gap, is one viewing.
# Longer than that and it is a new one.
PLAY_GAP = timedelta(minutes=10)
# The reporter runs every minute. Credit at most this much per report, so a
# reporter that was down for an hour does not book an hour of watching.
MAX_CREDIT_S = 120
DISK_LOW_FRACTION = 0.10
HISTORY_MAX_ROWS = 1000

# What becomes an issue. Both are P2 (error): a media problem may never be P1, so
# it can never wake the phone. The message is fixed text so every occurrence
# groups into one issue; the numbers ride in `extra`.
PROBLEMS = {
    "down": ("MediaServerDown", "the media server is not responding", "media", "server"),
    "disk_low": ("MediaDiskLow", "the media disk is nearly full", "media", "disk"),
}


def _conditions(up: bool, free: int | None, total: int | None) -> set[str]:
    bad = set()
    if not up:
        bad.add("down")
    if total and free is not None and free < DISK_LOW_FRACTION * total:
        bad.add("disk_low")
    return bad


def _event(key: str, payload: MediaReportIn) -> EventIn:
    exception_type, message, module, function = PROBLEMS[key]
    return EventIn(
        exception_type=exception_type,
        message=message,
        frames=[Frame(module=module, function=function)],
        level="error",
        environment="home",
        extra={
            "up": payload.up,
            "host_disk_free_mb": payload.host_disk_free_mb,
            "host_disk_total_mb": payload.host_disk_total_mb,
        },
    )


async def _record_plays(session: AsyncSession, site: Site, sessions: list[MediaSessionIn], now: datetime) -> None:
    """Write or extend one MediaPlay per active session. Never raises into the report."""
    for s in sessions:
        play = await session.scalar(
            select(MediaPlay)
            .where(
                MediaPlay.site_id == site.id,
                MediaPlay.session_id == s.session_id,
                MediaPlay.item_id == s.item_id,
                MediaPlay.last_seen_at >= now - PLAY_GAP,
            )
            .order_by(MediaPlay.last_seen_at.desc())
            .limit(1)
        )
        if play is None:
            session.add(
                MediaPlay(
                    site_id=site.id,
                    session_id=s.session_id,
                    item_id=s.item_id,
                    user_name=s.user,
                    title=s.title,
                    series=s.series,
                    episode=s.episode,
                    kind=s.kind,
                    client=s.client,
                    device=s.device,
                    play_method=s.play_method,
                    started_at=now,
                    last_seen_at=now,
                    watched_s=0,
                    position_s=s.position_s,
                    runtime_s=s.runtime_s,
                )
            )
            continue
        if not s.paused:
            gap = int((now - play.last_seen_at).total_seconds())
            play.watched_s += max(0, min(gap, MAX_CREDIT_S))
        play.last_seen_at = now
        play.position_s = s.position_s
        play.runtime_s = s.runtime_s
        # a stream can switch between direct play and transcoding partway through
        play.play_method = s.play_method


@ingest_router.post("/{ingest_key}/media", response_model=MediaAck, status_code=201)
async def record_media(
    ingest_key: str,
    request: Request,
    x_hub_signature: str | None = Header(default=None),
    x_hub_timestamp: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> MediaAck:
    raw = await request.body()
    site = await _load_site(session, ingest_key)
    _verify(site_secret(site), raw, x_hub_signature, x_hub_timestamp)
    try:
        payload = MediaReportIn.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc

    now = datetime.now(UTC)
    rows = (
        await session.scalars(
            select(MediaReport)
            .where(MediaReport.site_id == site.id, MediaReport.reported_at >= now - timedelta(minutes=25))
            .order_by(MediaReport.reported_at)
        )
    ).all()
    prev = rows[-1] if rows else None
    now_bad = _conditions(payload.up, payload.host_disk_free_mb, payload.host_disk_total_mb)
    was_bad = _conditions(prev.up, prev.host_disk_free_mb, prev.host_disk_total_mb) if prev else set()

    report = MediaReport(
        site_id=site.id,
        up=payload.up,
        version=payload.version,
        host_disk_total_mb=payload.host_disk_total_mb,
        host_disk_free_mb=payload.host_disk_free_mb,
        sessions=[s.model_dump() for s in payload.sessions],
    )
    session.add(report)
    try:
        await _record_plays(session, site, payload.sessions, now)
    except Exception:
        # The history is a nice-to-have next to the live view. It must never turn a
        # good report into a failed one.
        log.exception("could not record plays")
        await session.rollback()
        session.add(report)
    await session.commit()
    # Read the id NOW: a rollback below expires every object, and a lazy load on an
    # async session raises. See the same note in hub/stacks.py.
    report_id = report.id

    # Edge-triggered: only a problem that has just STARTED opens an event.
    await open_new(session, site, now_bad - was_bad, lambda key: _event(key, payload))

    try:
        timeline = [
            (r.reported_at, _conditions(r.up, r.host_disk_free_mb, r.host_disk_total_mb)) for r in rows
        ] + [(now, now_bad)]
        await auto_resolve(
            session, site, PROBLEMS, lambda key: _event(key, payload), timeline, now, now_bad,
            "the media server has been healthy",
        )
    except Exception:
        log.exception("could not auto-resolve media problems")
        await session.rollback()
    return MediaAck(id=report_id)


@router.get("/media", response_model=list[MediaOut])
async def list_media(session: AsyncSession = Depends(get_session)) -> list[MediaOut]:
    now = datetime.now(UTC)
    # Newest report per media site, however old, so a dead reporter is still listed
    # (as stale) instead of vanishing.
    latest = (
        await session.scalars(
            select(MediaReport)
            .distinct(MediaReport.site_id)
            .order_by(MediaReport.site_id, MediaReport.reported_at.desc())
        )
    ).all()
    if not latest:
        return []
    sites = {
        s.id: s.name
        for s in await session.scalars(select(Site).where(Site.id.in_([r.site_id for r in latest])))
    }
    out = []
    for r in latest:
        stale = now - r.reported_at > STALE_AFTER
        out.append(
            MediaOut(
                site=sites[r.site_id],
                stale=stale,
                reported_at=r.reported_at,
                up=r.up,
                version=r.version,
                host_disk_total_mb=r.host_disk_total_mb,
                host_disk_free_mb=r.host_disk_free_mb,
                sessions=[] if stale else [MediaSessionIn(**s) for s in r.sessions],
            )
        )
    out.sort(key=lambda o: o.site)
    return out


@router.get("/media/history", response_model=list[MediaPlayOut])
async def media_history(
    days: int = Query(default=7, ge=1),
    limit: int = Query(default=200, ge=1, le=HISTORY_MAX_ROWS),
    session: AsyncSession = Depends(get_session),
) -> list[MediaPlayOut]:
    days = min(days, settings.media_history_days)
    rows = await session.scalars(
        select(MediaPlay)
        .where(MediaPlay.started_at >= datetime.now(UTC) - timedelta(days=days))
        .order_by(MediaPlay.started_at.desc())
        .limit(limit)
    )
    return [
        MediaPlayOut(
            id=p.id,
            user=p.user_name,
            title=p.title,
            series=p.series,
            episode=p.episode,
            kind=p.kind,
            client=p.client,
            device=p.device,
            play_method=p.play_method,
            started_at=p.started_at,
            last_seen_at=p.last_seen_at,
            watched_s=p.watched_s,
            runtime_s=p.runtime_s,
        )
        for p in rows
    ]
