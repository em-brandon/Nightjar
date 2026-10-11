import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal
from .rollup import rollup
from .uptime import reconcile
from .models import Check, HomeReport, MediaPlay, MediaReport, Presence, Site, SpeedTest, StackReport

log = logging.getLogger("nightjar.probe")

# The ribbon shows 90 days; keep a little more so a boundary day is never half
# empty. Pruned once every PRUNE_EVERY rounds rather than on each one.
RETENTION = timedelta(days=100)
PRUNE_EVERY = 60
# Stack reports are a live view, not history: a week is plenty.
STACK_RETENTION = timedelta(days=7)
MEDIA_REPORT_RETENTION = timedelta(days=7)
HOME_REPORT_RETENTION = timedelta(days=7)
SPEED_RETENTION = timedelta(days=90)
PRESENCE_RETENTION = timedelta(days=30)


async def _probe(client: httpx.AsyncClient, site_id: int, url: str) -> Check:
    started = time.perf_counter()
    try:
        res = await client.get(url)
    except httpx.HTTPError as exc:
        # Class name only. The message can contain the URL, and a URL can
        # carry a token.
        return Check(site_id=site_id, ok=False, error=type(exc).__name__)
    ms = round((time.perf_counter() - started) * 1000)
    ok = res.status_code < 400
    return Check(
        site_id=site_id,
        ok=ok,
        status_code=res.status_code,
        latency_ms=ms,
        error=None if ok else f"HTTP {res.status_code}",
    )


async def run_round(client: httpx.AsyncClient) -> int:
    async with SessionLocal() as session:
        targets = (
            await session.execute(select(Site.id, Site.url).where(Site.url.is_not(None)))
        ).all()
        if not targets:
            return 0
        # Probe concurrently so one slow site cannot stretch the round and
        # skew the timestamps of every other site's check.
        results = await asyncio.gather(*(_probe(client, sid, url) for sid, url in targets))
        session.add_all(results)
        await session.commit()
        # An outage becomes an issue, and so an alert. It never raises: the probe must keep probing.
        await reconcile(session, [sid for sid, _ in targets])
        return len(results)


async def _prune() -> None:
    cutoff = datetime.now(UTC) - RETENTION
    async with SessionLocal() as session:
        # History is written before the raw rows go. If the rollup fails, the checks stay until it
        # succeeds: deleting them anyway would lose the only copy of those days.
        try:
            await rollup(session)
        except Exception:
            log.exception("daily rollup failed; keeping the raw checks until it succeeds")
            await session.rollback()
        else:
            await session.execute(delete(Check).where(Check.checked_at < cutoff))
        await session.execute(
            delete(StackReport).where(
                StackReport.reported_at < datetime.now(UTC) - STACK_RETENTION
            )
        )
        await session.execute(
            delete(MediaReport).where(
                MediaReport.reported_at < datetime.now(UTC) - MEDIA_REPORT_RETENTION
            )
        )
        now = datetime.now(UTC)
        await session.execute(delete(HomeReport).where(HomeReport.reported_at < now - HOME_REPORT_RETENTION))
        await session.execute(delete(SpeedTest).where(SpeedTest.tested_at < now - SPEED_RETENTION))
        await session.execute(delete(Presence).where(Presence.created_at < now - PRESENCE_RETENTION))
        # The viewing history is personal data, so it has its own, configurable limit.
        await session.execute(
            delete(MediaPlay).where(
                MediaPlay.started_at < datetime.now(UTC) - timedelta(days=settings.media_history_days)
            )
        )
        await session.commit()


async def probe_loop() -> None:
    """Runs for the life of the app. Nothing in here may escape and kill it:
    a watchdog that dies quietly looks exactly like a healthy site."""
    rounds = 0
    async with httpx.AsyncClient(
        timeout=settings.probe_timeout_s, follow_redirects=True
    ) as client:
        while True:
            try:
                await run_round(client)
                rounds += 1
                if rounds % PRUNE_EVERY == 0:
                    await _prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("probe round failed; will retry next interval")
            await asyncio.sleep(settings.probe_interval_s)
