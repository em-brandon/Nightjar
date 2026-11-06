import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import fingerprint as fp
from . import outbox, sources
from .config import settings
from .db import get_session
from .derive import site_secret
from .models import Event, Issue, Site
from .schemas import EventIn, IngestOut

router = APIRouter(prefix="/ingest", tags=["ingest"])

SEVERITY_BY_LEVEL = {"fatal": "P1", "error": "P2", "warning": "P3"}


def _signed_bytes(body: bytes, timestamp: str | None) -> bytes:
    """What the signature covers: the body alone (the original scheme), or the timestamp, a dot and the body. Putting the
    timestamp inside the signature is what stops a captured request being replayed later, or a fresh timestamp being
    attached to an old capture: either changes the bytes and the signature no longer matches."""
    return body if timestamp is None else timestamp.encode() + b"." + body


def _verify(secret: str, body: bytes, signature: str | None, timestamp: str | None = None) -> None:
    if not settings.verify_signatures:
        return
    if not signature:
        raise HTTPException(401, "missing signature")
    if timestamp is None:
        if settings.require_signed_timestamp:
            raise HTTPException(401, "missing timestamp")
    else:
        # Digits only and short, so int() cannot be fed something strange, then a window either side of now.
        if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
            raise HTTPException(401, "bad timestamp")
        if abs(time.time() - int(timestamp)) > settings.signature_max_age_s:
            raise HTTPException(401, "stale timestamp")
    expected = "sha256=" + hmac.new(secret.encode(), _signed_bytes(body, timestamp), hashlib.sha256).hexdigest()
    # As bytes: compare_digest raises TypeError on a str holding non-ASCII text, and a header is attacker-controlled, so
    # a junk signature must be a 401 and not a 500.
    if not hmac.compare_digest(expected.encode(), signature.encode("utf-8", "replace")):
        raise HTTPException(401, "bad signature")


async def verify_and_note(
    session: AsyncSession,
    request: Request,
    site: Site,
    raw: bytes,
    signature: str | None,
    timestamp: str | None = None,
) -> None:
    """_verify, plus a record of where the request came from (or why it was refused). Every signed route calls this."""
    ip = sources.client_ip(request)
    try:
        _verify(site_secret(site), raw, signature, timestamp)
    except HTTPException as exc:
        if exc.status_code == 401:
            await sources.note_failure(site, ip, str(exc.detail))
        raise
    if settings.verify_signatures:
        await sources.note_source(session, site, ip)


async def _load_site(session: AsyncSession, ingest_key: str) -> Site:
    site = await session.scalar(select(Site).where(Site.ingest_key == ingest_key))
    if site is None:
        raise HTTPException(404, "unknown site")
    return site


async def _is_spike(session: AsyncSession, issue_id: int) -> bool:
    since = datetime.now(UTC) - timedelta(minutes=settings.spike_window_min)
    recent = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.issue_id == issue_id, Event.occurred_at >= since)
    )
    return (recent or 0) >= settings.spike_threshold


@router.post("/{ingest_key}/event", response_model=IngestOut)
async def ingest_event(
    ingest_key: str,
    request: Request,
    x_hub_signature: str | None = Header(default=None),
    x_hub_timestamp: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> IngestOut:
    raw = await request.body()
    site = await _load_site(session, ingest_key)
    await verify_and_note(session, request, site, raw, x_hub_signature, x_hub_timestamp)

    try:
        payload = EventIn.model_validate_json(raw)
    except ValidationError as exc:
        # a bad body is the reporter's mistake to read about, not a server error to retry (the other ingest routes do the same)
        raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False)) from exc
    return await record_event(session, site, payload)


async def record_event(session: AsyncSession, site: Site, payload: EventIn) -> IngestOut:
    """Fingerprint, group and store one event. Shared by the HTTP endpoint and by
    the hub's own checks (staging stacks), so every source is deduplicated the
    same way and returns the same `state_change`."""
    frames = [f.model_dump(exclude_none=True) for f in payload.frames]

    digest = fp.compute(site.id, payload.exception_type, payload.message, frames)
    now = payload.occurred_at or datetime.now(UTC)

    # Lock the issue row so a burst of identical events cannot race each other
    # into two rows or double-count. Serialising here is correct and cheap.
    issue = await session.scalar(
        select(Issue)
        .where(Issue.site_id == site.id, Issue.fingerprint == digest)
        .with_for_update()
    )

    state_change: str | None = None

    if issue is None:
        title = payload.exception_type
        if payload.message:
            title = f"{payload.exception_type}: {payload.message[:200]}"
        issue = Issue(
            site_id=site.id,
            fingerprint=digest,
            title=title,
            culprit=fp.culprit_of(frames),
            level=payload.level,
            severity=SEVERITY_BY_LEVEL.get(payload.level, "P2"),
            status="open",
            event_count=1,
            first_seen=now,
            last_seen=now,
        )
        session.add(issue)
        await session.flush()
        state_change = "new"
    else:
        previous_status = issue.status
        snooze_expired = (
            previous_status == "snoozed"
            and issue.snoozed_until is not None
            and issue.snoozed_until <= now
        )
        issue.event_count += 1
        issue.last_seen = now

        if previous_status == "muted":
            # muted issues keep counting but never speak again until unmuted
            pass
        elif previous_status == "resolved" or snooze_expired:
            issue.status = "open"
            issue.snoozed_until = None
            state_change = "regression"

    session.add(
        Event(
            issue_id=issue.id,
            payload=payload.model_dump(mode="json", exclude_none=True),
            occurred_at=now,
        )
    )

    # The spike check counts this event, so it is flushed first. It also happens BEFORE the commit:
    # the notification for a state change is written in the same transaction that records it, so
    # either both exist or neither does and a restart between the two loses nothing (decision #56).
    await session.flush()
    if state_change is None and issue.status == "open" and await _is_spike(session, issue.id):
        state_change = "spike"
    if state_change:
        wall = datetime.now(UTC)
        # a spike's time of change is its window, so one window is one alert however many events it holds
        changed_at = outbox.window_start(wall, settings.spike_window_min) if state_change == "spike" else now
        await outbox.enqueue(session, issue, site.name, state_change, changed_at, wall)
    await session.commit()

    return IngestOut(
        issue_id=issue.id,
        fingerprint=digest,
        event_count=issue.event_count,
        severity=issue.severity,
        state_change=state_change,
    )
