import hmac
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .models import Presence
from .schemas import PresenceIn, PresenceOut

# Presence controls WHICH CHANNELS fire, never WHETHER something alerts (CLAUDE.md). Nothing here
# can suppress an alert, and nothing reads it yet: chunk 2's alert policy will. For now it is shown.
#
# It is stored with an expiry, not as a toggle: a toggle desyncs for good the first time a tap is
# missed. On expiry, or with no record at all, the effective state is AWAY: the state where only
# the reliable channel is used. Always fail toward the reliable channel.
public_router = APIRouter(tags=["presence"])
router = APIRouter(prefix="/api", tags=["presence"])


def effective(row: Presence | None, now: datetime) -> PresenceOut:
    if row is None:
        return PresenceOut(state="away", source=None, expires_at=None, set_at=None, recorded=False)
    if row.state == "home" and row.expires_at <= now:
        # the tap has expired: nobody has confirmed they are home
        return PresenceOut(state="away", source="expired", expires_at=row.expires_at, set_at=row.created_at, recorded=True)
    return PresenceOut(state=row.state, source=row.source, expires_at=row.expires_at, set_at=row.created_at, recorded=True)


async def _current(session: AsyncSession) -> Presence | None:
    return await session.scalar(select(Presence).order_by(Presence.created_at.desc(), Presence.id.desc()).limit(1))


async def effective_state(session: AsyncSession, now: datetime) -> str:
    """"home" or "away" right now, for the alert policy. Away on expiry or with no record at all."""
    return effective(await _current(session), now).state


async def _set(session: AsyncSession, body: PresenceIn) -> PresenceOut:
    now = datetime.now(UTC)
    row = Presence(state=body.state, source=body.source, expires_at=now + timedelta(minutes=body.ttl_minutes))
    session.add(row)
    await session.commit()
    return effective(await _current(session), datetime.now(UTC))


@public_router.post("/presence", response_model=PresenceOut)
async def tap(body: PresenceIn, session: AsyncSession = Depends(get_session)) -> PresenceOut:
    """Called by an iOS Shortcuts NFC automation. Authenticated by a shared token in the body."""
    if not settings.presence_token:
        raise HTTPException(503, "presence is not configured (set PRESENCE_TOKEN)")
    # As bytes: compare_digest raises TypeError (a 500) on a str holding non-ASCII text, and this token comes from the caller
    if not body.token or not hmac.compare_digest(body.token.encode(), settings.presence_token.encode()):
        raise HTTPException(401, "bad token")
    return await _set(session, body)


@router.post("/presence", response_model=PresenceOut)
async def set_from_console(body: PresenceIn, session: AsyncSession = Depends(get_session)) -> PresenceOut:
    """The console's own switch (behind the console token), for setting it by hand."""
    return await _set(session, body.model_copy(update={"source": "manual"}))


@router.get("/presence", response_model=PresenceOut)
async def get_presence(session: AsyncSession = Depends(get_session)) -> PresenceOut:
    return effective(await _current(session), datetime.now(UTC))
