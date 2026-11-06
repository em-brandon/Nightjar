"""People who can sign in to the console, and how a new one is brought in (decision #59).

An owner creates an account and is shown a one-time setup token. There is no email sender, so nothing is mailed: the owner
hands the token over. The person pastes it into the sign-in page, chooses a password, scans an authenticator secret and
proves it works with a code. Until then the account cannot sign in at all.

  /api/users            owner only: list, create, change role, disable, reset
  /api/auth/setup/...   public, authenticated by the setup token itself, and rate limited like a login

Rules that keep this from locking everyone out, or letting someone in:
  * the last active owner can be neither demoted nor disabled, and nobody changes their own role or status here
    (an owner who needs that has the command line, hub/operator_cli.py)
  * disabling or resetting ends the person's sessions at once
  * a setup token is shown once, stored as a hash, and expires
  * accounts are never deleted: the audit trail names people
"""

import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, auth, credentials
from .config import settings
from .db import get_session
from .models import ConsoleSession, Operator
from .sources import client_ip

SETUP_TOKEN_PREFIX = "njs_"
SETUP_TTL = timedelta(hours=48)
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,38}[a-z0-9]")

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(auth.require_owner)])
setup_router = APIRouter(prefix="/api/auth/setup", tags=["auth"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_setup_token() -> tuple[str, str, datetime]:
    token = SETUP_TOKEN_PREFIX + credentials.new_session_token()[0]
    return token, credentials.hash_token(token), _now() + SETUP_TTL


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    disabled: bool
    # created but not yet set up by its person: cannot sign in
    pending_setup: bool
    setup_expires_at: datetime | None
    created_at: datetime


def _out(op: Operator) -> UserOut:
    return UserOut(
        id=op.id,
        username=op.username,
        role=op.role,
        disabled=op.disabled_at is not None,
        pending_setup=op.setup_token_hash is not None,
        setup_expires_at=op.setup_expires_at if op.setup_token_hash else None,
        created_at=op.created_at,
    )


class UserCreate(BaseModel):
    username: str = Field(max_length=40)
    role: str = Field(default="viewer", max_length=12)


class UserPatch(BaseModel):
    role: str | None = Field(default=None, max_length=12)
    disabled: bool | None = None


class SetupIssued(BaseModel):
    user: UserOut
    # shown here and never again
    setup_token: str
    expires_at: datetime


def _check_role(role: str) -> None:
    if role not in auth.ROLES:
        raise HTTPException(422, f"role must be one of: {', '.join(auth.ROLES)}")


async def _active_owners_besides(db: AsyncSession, op: Operator) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(Operator)
            .where(
                Operator.id != op.id,
                Operator.role == "owner",
                Operator.disabled_at.is_(None),
                Operator.setup_token_hash.is_(None),
            )
        )
        or 0
    )


async def _end_sessions(db: AsyncSession, operator_id: int) -> int:
    result = await db.execute(
        update(ConsoleSession)
        .where(ConsoleSession.operator_id == operator_id, ConsoleSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    return result.rowcount


@router.get("", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_session)) -> list[UserOut]:
    return [_out(o) for o in await db.scalars(select(Operator).order_by(Operator.id))]


@router.post("", response_model=SetupIssued, status_code=201)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_session)) -> SetupIssued:
    username = body.username.strip().lower()
    if not USERNAME.fullmatch(username):
        raise HTTPException(422, "a username is 3 to 40 letters, digits, dots, dashes or underscores")
    _check_role(body.role)
    if await db.scalar(select(Operator.id).where(Operator.username == username)):
        raise HTTPException(409, "that username is taken")
    token, token_hash, expires = _new_setup_token()
    op = Operator(
        username=username, password_hash="", role=body.role, totp_version=1, totp_last_step=0,
        recovery_hashes=[], setup_token_hash=token_hash, setup_expires_at=expires,
    )
    db.add(op)
    audit.record_login(db, "user_created", f"{username} as {body.role}")
    await db.commit()
    return SetupIssued(user=_out(op), setup_token=token, expires_at=expires)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int, body: UserPatch, request: Request, db: AsyncSession = Depends(get_session)
) -> UserOut:
    op = await db.scalar(select(Operator).where(Operator.id == user_id).with_for_update())
    if op is None:
        raise HTTPException(404, "unknown user")
    who: auth.Who = request.state.who
    if op.id == who.operator_id:
        raise HTTPException(409, "you cannot change your own role or status here: ask another owner")
    if body.role is not None:
        _check_role(body.role)
    losing_owner = (body.role is not None and body.role != "owner") or body.disabled is True
    if op.role == "owner" and op.disabled_at is None and losing_owner and not await _active_owners_besides(db, op):
        raise HTTPException(409, "that is the last active owner: make another owner first")
    if body.role is not None and body.role != op.role:
        audit.record_login(db, "role_changed", f"{op.username}: {op.role} -> {body.role}")
        op.role = body.role
    if body.disabled is True and op.disabled_at is None:
        op.disabled_at = _now()
        ended = await _end_sessions(db, op.id)
        audit.record_login(db, "user_disabled", f"{op.username}, {ended} session(s) ended")
    elif body.disabled is False and op.disabled_at is not None:
        op.disabled_at = None
        audit.record_login(db, "user_enabled", op.username)
    await db.commit()
    return _out(op)


@router.post("/{user_id}/reset", response_model=SetupIssued)
async def reset_user(user_id: int, request: Request, db: AsyncSession = Depends(get_session)) -> SetupIssued:
    """For a forgotten password or a lost phone: clears the password and authenticator, ends the sessions and issues a
    new setup token, exactly as for a new account."""
    op = await db.scalar(select(Operator).where(Operator.id == user_id).with_for_update())
    if op is None:
        raise HTTPException(404, "unknown user")
    who: auth.Who = request.state.who
    if op.id == who.operator_id:
        raise HTTPException(409, "reset your own account from the server: python -m hub.operator_cli reset-2fa / passwd")
    if op.role == "owner" and op.disabled_at is None and not await _active_owners_besides(db, op):
        raise HTTPException(409, "that is the last active owner")
    token, token_hash, expires = _new_setup_token()
    op.password_hash = ""
    op.recovery_hashes = []
    op.totp_version += 1
    op.totp_last_step = 0
    op.setup_token_hash, op.setup_expires_at = token_hash, expires
    ended = await _end_sessions(db, op.id)
    audit.record_login(db, "user_reset", f"{op.username}, {ended} session(s) ended")
    await db.commit()
    return SetupIssued(user=_out(op), setup_token=token, expires_at=expires)


# ---------------------------------------------------------------------------------------------- setup (public)
class SetupStart(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=200)


class SetupStarted(BaseModel):
    username: str
    # for an authenticator app that cannot scan: the secret, and the otpauth:// link
    totp_secret: str
    totp_uri: str
    # shown once, each works once in place of an authenticator code
    recovery_codes: list[str]


class SetupConfirm(BaseModel):
    token: str = Field(max_length=200)
    code: str = Field(max_length=32)


async def _pending(db: AsyncSession, request: Request, token: str) -> Operator:
    """The account this setup token belongs to. Every miss counts against the caller's address, like a wrong password."""
    ip = client_ip(request)
    window = auth._ip_failures[ip]
    if auth._recent(window, time.monotonic()) >= auth.IP_FAIL_LIMIT:
        raise HTTPException(429, "too many failed attempts; wait a few minutes")
    op = await db.scalar(
        select(Operator).where(Operator.setup_token_hash == credentials.hash_token(token)).with_for_update()
    )
    if op is None or op.disabled_at is not None or op.setup_expires_at is None or op.setup_expires_at < _now():
        window.append(time.monotonic())
        raise HTTPException(404, "that setup token is not valid, or has expired: ask an owner for a new one")
    if not settings.secret_master_key:
        raise HTTPException(503, "the hub has no SECRET_MASTER_KEY, which the authenticator code needs")
    return op


@setup_router.post("/start", response_model=SetupStarted)
async def setup_start(body: SetupStart, request: Request, db: AsyncSession = Depends(get_session)) -> SetupStarted:
    op = await _pending(db, request, body.token)
    problem = credentials.password_problem(body.password)
    if problem:
        raise HTTPException(422, f"password: {problem}")
    codes = credentials.new_recovery_codes()
    op.password_hash = credentials.hash_password(body.password)
    op.recovery_hashes = [credentials.hash_recovery(c) for c in codes]
    await db.commit()
    secret = credentials.totp_secret(settings.secret_master_key, op.id, op.totp_version)
    return SetupStarted(
        username=op.username,
        totp_secret=credentials.totp_base32(secret),
        totp_uri=credentials.totp_uri(secret, op.username),
        recovery_codes=codes,
    )


@setup_router.post("/confirm")
async def setup_confirm(body: SetupConfirm, request: Request, db: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    op = await _pending(db, request, body.token)
    if not op.password_hash:
        raise HTTPException(409, "choose a password first")
    secret = credentials.totp_secret(settings.secret_master_key, op.id, op.totp_version)
    step = credentials.totp_match(secret, body.code, op.totp_last_step)
    if step is None:
        auth._ip_failures[client_ip(request)].append(time.monotonic())
        raise HTTPException(422, "that is not the current code: check the app's clock and try again")
    op.totp_last_step = step
    op.setup_token_hash = None
    op.setup_expires_at = None
    audit.record_login(db, "user_activated", op.username, actor=op.username)
    await db.commit()
    return {"ok": True}
