"""Where signed reports come from, and which ones were refused (docs/KEYS.md, "Noticing a leak").

The hub cannot tell a forged report signed with a leaked secret from a real one, so these two records are the only
evidence: refused requests (someone has the key but not the secret, or an app is holding a rotated secret) and a first
report from an address the site has never used. Both are shown in the console and in a site's history. Neither sends a
notification: a guess that wakes the phone is a bug (CLAUDE.md), and a site that moved host looks the same.

The address is read from CF-Connecting-IP, then X-Forwarded-For, as the browser route does. Behind Caddy that is the
real caller; with the hub reachable directly a caller can write anything there, so the address is evidence, not proof.
Recording must never break ingest: every path here is wrapped.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import record
from .db import SessionLocal
from .models import AuditLog, IngestFailure, IngestSource, Site

log = logging.getLogger("nightjar.sources")

WRITE_EVERY_S = 60  # one write per (site, address) a minute
MAX_REMEMBERED = 10_000  # a caller who varies the header cannot grow this without bound
AUDIT_PER_HOUR = 5  # refused-request lines in a site's history per hour; the counts keep going
KEEP_FAILURES_DAYS = 30

_last_written: dict[tuple[int, str], float] = {}


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")
    return ip[:64]


def _hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


async def note_source(session: AsyncSession, site: Site, ip: str) -> None:
    """Remember that this address reported for this site, in the caller's transaction. The first address a site is ever
    seen from is its baseline; each different one after that adds a "new_source" line to the site's history."""
    try:
        key = (site.id, ip)
        now_m = time.monotonic()
        last = _last_written.get(key)
        if last is not None and now_m - last < WRITE_EVERY_S:
            return
        if len(_last_written) >= MAX_REMEMBERED:
            _last_written.clear()
        _last_written[key] = now_m

        now = datetime.now(UTC)
        stmt = insert(IngestSource).values(site_id=site.id, ip=ip, first_seen_at=now, last_seen_at=now, reports=1)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_ingest_source",
            set_={"last_seen_at": now, "reports": IngestSource.reports + 1},
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        inserted = (await session.execute(stmt)).scalar_one()
        if inserted:
            others = await session.scalar(
                select(func.count()).select_from(IngestSource).where(IngestSource.site_id == site.id, IngestSource.ip != ip)
            )
            if others:
                record(session, site, "new_source", ip)
    except Exception:
        log.exception("could not record a source")


async def note_failure(site: Site, ip: str, reason: str) -> None:
    """Count a refused request. Uses its own session, because the request is about to fail and roll back the caller's."""
    try:
        now = datetime.now(UTC)
        bucket = _hour(now)
        async with SessionLocal() as session:
            stmt = insert(IngestFailure).values(site_id=site.id, ip=ip, reason=reason, bucket=bucket, count=1)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_ingest_failure", set_={"count": IngestFailure.count + 1}
            ).returning(literal_column("(xmax = 0)").label("inserted"))
            first_this_hour = (await session.execute(stmt)).scalar_one()
            if first_this_hour:
                lines = await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.site_id == site.id,
                        AuditLog.action == "refused_request",
                        AuditLog.created_at >= now - timedelta(hours=1),
                    )
                )
                if (lines or 0) < AUDIT_PER_HOUR:
                    record(session, site, "refused_request", f"{ip}: {reason}")
                await session.execute(
                    delete(IngestFailure).where(IngestFailure.bucket < now - timedelta(days=KEEP_FAILURES_DAYS))
                )
            await session.commit()
    except Exception:
        log.exception("could not record a refused request")


async def failures_24h(session: AsyncSession) -> dict[int, int]:
    since = datetime.now(UTC) - timedelta(hours=24)
    rows = await session.execute(
        select(IngestFailure.site_id, func.sum(IngestFailure.count))
        .where(IngestFailure.bucket >= _hour(since))
        .group_by(IngestFailure.site_id)
    )
    return {site_id: int(total) for site_id, total in rows.all()}


async def new_source_times(session: AsyncSession, days: int = 7) -> dict[int, datetime]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await session.execute(
        select(AuditLog.site_id, func.max(AuditLog.created_at))
        .where(AuditLog.action == "new_source", AuditLog.created_at >= since, AuditLog.site_id.is_not(None))
        .group_by(AuditLog.site_id)
    )
    return {site_id: at for site_id, at in rows.all()}
