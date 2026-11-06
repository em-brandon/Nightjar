"""Device tokens: credentials for machines (decision #59).

The wall display, a CI job and a script each get their own token instead of sharing one secret that can do everything.
An owner makes them here, sees which have been used and from where, and revokes one without touching the others.

  * the token is shown once, at creation, and stored only as a SHA-256 hash
  * `read` acts as a viewer, `write` as a maintainer. A device is never an owner, so a leaked token can read or do the
    day-to-day work but cannot create a site, rotate a secret, or issue another credential
  * a token may expire; revoking it ends the sessions a wall display opened with it, on their next request
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, auth, credentials
from .db import get_session
from .models import ConsoleSession, DeviceToken, Operator

router = APIRouter(prefix="/api/devices", tags=["devices"], dependencies=[Depends(auth.require_owner)])

MAX_ACTIVE = 50  # a limit that would only ever be reached by a loop gone wrong


class DeviceOut(BaseModel):
    id: int
    name: str
    # the token's first characters: tells two tokens apart in a list, and is not enough to use
    prefix: str
    scope: Literal["read", "write"]
    created_by: str | None
    created_at: datetime
    last_used_at: datetime | None
    last_ip: str
    expires_at: datetime | None
    revoked_at: datetime | None
    # wall-display sessions currently open with this token
    open_sessions: int


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scope: Literal["read", "write"] = "read"
    # None: no expiry. A script that should stop working by itself gets a number of days.
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class DeviceIssued(BaseModel):
    device: DeviceOut
    # shown here and never again
    token: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _out(db: AsyncSession, row: DeviceToken) -> DeviceOut:
    creator = await db.scalar(select(Operator.username).where(Operator.id == row.created_by_id)) if row.created_by_id else None
    sessions = await db.scalar(
        select(func.count())
        .select_from(ConsoleSession)
        .where(
            ConsoleSession.device_token_id == row.id,
            ConsoleSession.revoked_at.is_(None),
            ConsoleSession.expires_at > _now(),
        )
    )
    return DeviceOut(
        id=row.id, name=row.name, prefix=row.prefix, scope=row.scope, created_by=creator, created_at=row.created_at,
        last_used_at=row.last_used_at, last_ip=row.last_ip, expires_at=row.expires_at, revoked_at=row.revoked_at,
        open_sessions=sessions or 0,
    )


@router.get("", response_model=list[DeviceOut])
async def list_devices(db: AsyncSession = Depends(get_session)) -> list[DeviceOut]:
    rows = await db.scalars(select(DeviceToken).order_by(DeviceToken.revoked_at.is_not(None), DeviceToken.id.desc()))
    return [await _out(db, r) for r in rows]


@router.post("", response_model=DeviceIssued, status_code=201)
async def create_device(body: DeviceCreate, request: Request, db: AsyncSession = Depends(get_session)) -> DeviceIssued:
    who: auth.Who = request.state.who
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "give the device a name, so you can tell what to revoke")
    active = await db.scalar(select(func.count()).select_from(DeviceToken).where(DeviceToken.revoked_at.is_(None)))
    if (active or 0) >= MAX_ACTIVE:
        raise HTTPException(409, f"{MAX_ACTIVE} active device tokens is the limit: revoke some first")
    token, token_hash, prefix = credentials.new_device_token()
    row = DeviceToken(
        name=name, prefix=prefix, token_hash=token_hash, scope=body.scope, created_by_id=who.operator_id,
        expires_at=_now() + timedelta(days=body.expires_days) if body.expires_days else None,
    )
    db.add(row)
    # the token itself is never written to the audit trail
    audit.record_login(db, "device_token_created", f"{name}, {body.scope}" + (f", {body.expires_days} days" if body.expires_days else ""))
    await db.commit()
    return DeviceIssued(device=await _out(db, row), token=token)


@router.delete("/{device_id}", response_model=DeviceOut)
async def revoke_device(device_id: int, db: AsyncSession = Depends(get_session)) -> DeviceOut:
    row = await db.get(DeviceToken, device_id)
    if row is None:
        raise HTTPException(404, "unknown device")
    if row.revoked_at is None:
        row.revoked_at = _now()
        ended = await db.execute(
            update(ConsoleSession)
            .where(ConsoleSession.device_token_id == row.id, ConsoleSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        audit.record_login(db, "device_token_revoked", f"{row.name}, {ended.rowcount} session(s) ended")
        await db.commit()
    return await _out(db, row)
