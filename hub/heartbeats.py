"""Heartbeats: alert on SILENCE.

A crash reporter can only speak when something runs. A cron job that stopped, a worker that hung, or a
private app on a network the probe cannot reach says nothing at all, and nothing at all looks like a quiet,
healthy day. A heartbeat turns that around: the thing pings every N seconds and the hub raises an issue when
the pings stop. Silence must never read as all-clear (CLAUDE.md, chunk 3).

  POST /ingest/{key}/heartbeat/{name}   {} or {"status": "ok"|"fail", "message": "...",
                                         "interval_s": 300, "grace_s": 60, "kind": "app"|"job"}

Signed like every other ingest, so a site's key can ping but cannot read the console.

Two rules every pushed source follows (hub/edge.py), applied here:

  * **Edge-triggered.** An issue opens when a problem STARTS and not on every round while it lasts. The probe
    loop runs this once a minute, so one event per round would be 1,440 a day and would trip the spike detector.
    `missed_open` and `failed_open` on the row are that memory.
  * **It resolves itself only after the pings have returned and stayed** (`pings_needed`). A thing that flaps
    must not open and close an issue all day; every relapse after a resolve is a regression and an alert.

Severity is derived from what the heartbeat IS, never chosen by hand: an `app` heartbeat stands in for uptime
(P1 in production), a `job` is P2. Staging is never P1 (nothing is down for a client).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .acks import dismiss_alerts
from .config import settings
from .db import SessionLocal, get_session
from .ingest import _load_site, record_event, verify_and_note
from .models import Heartbeat, Issue, IssueAction, Site
from .schemas import EventIn, Frame, HeartbeatIn, HeartbeatOut
from .stacks import _fingerprint

log = logging.getLogger("nightjar.heartbeats")

ingest_router = APIRouter(prefix="/ingest", tags=["heartbeats"])
router = APIRouter(prefix="/api", tags=["heartbeats"])

# Fixed text, never the name or the times, so one heartbeat is one issue however long it stays silent. The name
# goes in a frame, because the fingerprint normalises quoted strings and numbers out of a message.
MISSED = "HeartbeatMissed"
FAILED = "HeartbeatFailed"
MESSAGES = {
    MISSED: "no heartbeat arrived within its expected interval",
    FAILED: "the heartbeat reported that its run failed",
}

# Nothing is a heartbeat's name unless it can sit in a URL path without being escaped.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"


def default_grace(interval_s: int) -> int:
    """How late a ping may be before it is missed: a fifth of the interval, between a minute and an hour. A
    five-minute heartbeat gets a minute, a nightly job gets an hour."""
    return max(60, min(interval_s // 5, 3600))


def pings_needed(interval_s: int) -> int:
    """Good pings in a row before a problem resolves itself. A fast heartbeat can flap, so it needs three. A slow
    one (hourly or less often) cannot flap by the minute, and asking a nightly job for three would leave a fixed
    problem open for three days, so one is enough."""
    return 3 if interval_s < 3600 else 1


def deadline(hb: Heartbeat) -> datetime:
    return hb.last_ping_at + timedelta(seconds=hb.interval_s + hb.grace_s)


def state_of(hb: Heartbeat, now: datetime) -> Literal["ok", "late", "missed", "failed", "recovering"]:
    """What the console shows. `late` is past its interval but inside the grace: nothing alerts yet. `recovering` is
    pinging again but has not yet earned the issue's resolution (`pings_needed`), so a fixed job is not shown as
    still silent while its good pings count up."""
    if now > deadline(hb):
        return "missed"
    if hb.failed_open and hb.last_status == "fail":
        return "failed"
    if hb.missed_open or hb.failed_open:
        return "recovering"
    if now > hb.last_ping_at + timedelta(seconds=hb.interval_s):
        return "late"
    return "ok"


def make_event(site: Site, hb: Heartbeat, kind: str) -> EventIn:
    return EventIn(
        exception_type=kind,
        message=MESSAGES[kind],
        frames=[Frame(module="heartbeat", function=hb.name)],
        level="fatal" if hb.kind == "app" and site.env == "production" else "error",
        environment=site.env,
        extra={
            "heartbeat": hb.name,
            "interval_s": hb.interval_s,
            "last_ping_at": hb.last_ping_at.isoformat(),
            "message": hb.last_message,
        },
    )


async def _resolve(session: AsyncSession, site: Site, hb: Heartbeat) -> None:
    """Resolve the open issue for each problem this heartbeat has. Only `open` issues are touched: muted stays
    muted, snoozed stays snoozed, and one a person already resolved is not ours to change."""
    kinds = [k for k, flag in ((MISSED, hb.missed_open), (FAILED, hb.failed_open)) if flag]
    if kinds:
        digests = [_fingerprint(site, make_event(site, hb, k)) for k in kinds]
        # the same row lock ingest and the console's actions take
        issues = (
            await session.scalars(
                select(Issue)
                .where(Issue.site_id == site.id, Issue.fingerprint.in_(digests), Issue.status == "open")
                .with_for_update()
            )
        ).all()
        for issue in issues:
            issue.status = "resolved"
            issue.snoozed_until = None
            session.add(
                IssueAction(
                    issue_id=issue.id,
                    action="resolve",
                    detail=f"auto: {pings_needed(hb.interval_s)} good ping(s) in a row",
                )
            )
            await dismiss_alerts(session, issue.id)
    hb.missed_open = False
    hb.failed_open = False
    hb.good_streak = 0
    await session.commit()


async def record_ping(
    session: AsyncSession, site: Site, name: str, payload: HeartbeatIn, now: datetime | None = None
) -> Heartbeat:
    now = now or datetime.now(UTC)
    interval = payload.interval_s or settings.heartbeat_default_interval_s
    # Two first pings at once must not make two rows: the unique key decides, and the loser just reads.
    await session.execute(
        insert(Heartbeat)
        .values(
            site_id=site.id,
            name=name,
            kind=payload.kind or "job",
            interval_s=interval,
            grace_s=payload.grace_s if payload.grace_s is not None else default_grace(interval),
            last_ping_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_heartbeat_site_name")
    )
    hb = await session.scalar(
        select(Heartbeat).where(Heartbeat.site_id == site.id, Heartbeat.name == name).with_for_update()
    )
    assert hb is not None
    # The latest ping wins, so a schedule is changed by changing the cron line and nothing in the hub.
    if payload.kind:
        hb.kind = payload.kind
    if payload.interval_s:
        hb.interval_s = payload.interval_s
        if payload.grace_s is None:
            hb.grace_s = default_grace(payload.interval_s)
    if payload.grace_s is not None:
        hb.grace_s = payload.grace_s
    hb.last_ping_at = now
    hb.last_status = payload.status
    hb.last_message = payload.message

    if payload.status == "fail":
        hb.good_streak = 0
        if not hb.failed_open:
            # The event first, the flag after: if this dies between them the next failing ping opens it again,
            # and a duplicate event is far better than a failure nobody was told about.
            await record_event(session, site, make_event(site, hb, FAILED))
            hb.failed_open = True
        await session.commit()
    elif hb.missed_open or hb.failed_open:
        hb.good_streak += 1
        if hb.good_streak >= pings_needed(hb.interval_s):
            await _resolve(session, site, hb)
        else:
            await session.commit()
    else:
        await session.commit()
    return hb


async def reconcile(session: AsyncSession, now: datetime | None = None) -> int:
    """Open an issue for each heartbeat that has gone silent, once. Returns how many it opened. A failure for one
    heartbeat is logged and never stops the others, and never reaches the probe loop: a watchdog must keep
    watching."""
    ids = list((await session.scalars(select(Heartbeat.id))).all())
    await session.rollback()  # the read above opened a transaction; each heartbeat below takes its own lock
    opened = 0
    for hb_id in ids:
        try:
            hb = await session.scalar(select(Heartbeat).where(Heartbeat.id == hb_id).with_for_update())
            if hb is None or (now or datetime.now(UTC)) <= deadline(hb):
                await session.rollback()
                continue
            # silent again: whatever good pings it had since a problem began no longer count
            hb.good_streak = 0
            if not hb.missed_open:
                site = await session.get(Site, hb.site_id)
                await record_event(session, site, make_event(site, hb, MISSED))
                hb.missed_open = True
                opened += 1
            await session.commit()
        except Exception:
            log.exception("could not reconcile heartbeat %s", hb_id)
            await session.rollback()
    return opened


async def reconcile_round() -> None:
    """One pass in its own session, for the probe loop."""
    async with SessionLocal() as session:
        await reconcile(session)


def _out(hb: Heartbeat, site: Site, now: datetime) -> HeartbeatOut:
    return HeartbeatOut(
        id=hb.id,
        site=site.name,
        env=site.env,
        name=hb.name,
        kind=hb.kind,  # type: ignore[arg-type]
        interval_s=hb.interval_s,
        grace_s=hb.grace_s,
        last_ping_at=hb.last_ping_at,
        last_status=hb.last_status,  # type: ignore[arg-type]
        last_message=hb.last_message,
        state=state_of(hb, now),
    )


@ingest_router.post("/{ingest_key}/heartbeat/{name}", response_model=HeartbeatOut)
async def ping(
    ingest_key: str,
    request: Request,
    name: str = Path(pattern=NAME_PATTERN),
    x_hub_signature: str | None = Header(default=None),
    x_hub_timestamp: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> HeartbeatOut:
    raw = await request.body()
    site = await _load_site(session, ingest_key)
    await verify_and_note(session, request, site, raw, x_hub_signature, x_hub_timestamp)
    try:
        # a bare POST from a cron line has no body, and that is a valid "ok"
        payload = HeartbeatIn.model_validate_json(raw) if raw.strip() else HeartbeatIn()
    except ValidationError as exc:
        # 422 like any other bad input, not a 500 that a cron job would retry
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc
    hb = await record_ping(session, site, name, payload)
    return _out(hb, site, datetime.now(UTC))


@router.get("/heartbeats", response_model=list[HeartbeatOut])
async def list_heartbeats(session: AsyncSession = Depends(get_session)) -> list[HeartbeatOut]:
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(Heartbeat, Site).join(Site, Heartbeat.site_id == Site.id).order_by(Site.name, Heartbeat.name)
        )
    ).all()
    return [_out(hb, site, now) for hb, site in rows]


@router.delete("/heartbeats/{heartbeat_id}", status_code=204)
async def delete_heartbeat(heartbeat_id: int, session: AsyncSession = Depends(get_session)) -> None:
    """For a job that has been retired on purpose, so its silence stops being a problem. Any issue it already
    opened stays: resolving that is a person's call."""
    hb = await session.get(Heartbeat, heartbeat_id)
    if hb is None:
        raise HTTPException(404, "unknown heartbeat")
    await session.delete(hb)
    await session.commit()
