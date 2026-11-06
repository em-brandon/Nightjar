"""Passkeys: signing in with a phone, laptop or security key instead of a password and a code (decision #59).

A passkey proves two things at once, that you hold the device and (because we require user verification) that you
unlocked it with a fingerprint, face or PIN, so it replaces the password and the authenticator code together. The
password and code still work; a passkey is an addition, never the only way in, so losing a phone cannot lock the
owner out.

This is the first dependency the login code did not write itself (`webauthn`, which brings `cryptography`). The
protocol's signature and CBOR handling is exactly the kind of thing not to hand-roll.

  * credentials are discoverable ("resident"), so sign-in asks for no username and cannot be used to find out which
    usernames exist
  * only the public key is stored
  * challenges live in this process's memory, single use, five minutes. The hub is one process on purpose; if it ever
    is not, this store moves into the database with the rate limits (CLAUDE.md, Conventions)
"""

import asyncio
import json
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse, InvalidJSONStructure
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import audit, auth, credentials
from .config import settings
from .db import get_session
from .models import Operator, Passkey

# Everything that needs a signed-in person (list, register, remove)
router = APIRouter(prefix="/api/passkeys", tags=["passkeys"])
# Signing in with one needs no session yet
public_router = APIRouter(prefix="/api/auth/passkey", tags=["auth"])

CHALLENGE_TTL_S = 300
MAX_CHALLENGES = 500
MAX_PER_OPERATOR = 10
RP_NAME = "Nightjar"

# challenge id -> (challenge bytes, purpose, operator id for a registration, expires at on the monotonic clock)
_challenges: dict[str, tuple[bytes, str, int | None, float]] = {}


def rp_id() -> str:
    if settings.webauthn_rp_id:
        return settings.webauthn_rp_id
    host = urlparse(settings.hub_public_url).hostname if settings.hub_public_url else None
    return host or "localhost"


def origins() -> list[str]:
    if settings.webauthn_origins.strip():
        return [o.strip().rstrip("/") for o in settings.webauthn_origins.split(",") if o.strip()]
    if settings.hub_public_url:
        u = urlparse(settings.hub_public_url)
        return [f"{u.scheme}://{u.netloc}"]
    # local development: the Vite dev server, and the hub serving itself
    return ["http://localhost:5173", "http://localhost:8000"]


def _remember(challenge: bytes, purpose: str, operator_id: int | None) -> str:
    now = time.monotonic()
    for key in [k for k, v in _challenges.items() if v[3] < now]:
        del _challenges[key]
    if len(_challenges) >= MAX_CHALLENGES:
        # someone is asking for challenges and never answering: drop the oldest rather than grow without bound
        del _challenges[min(_challenges, key=lambda k: _challenges[k][3])]
    cid = secrets.token_urlsafe(24)
    _challenges[cid] = (challenge, purpose, operator_id, now + CHALLENGE_TTL_S)
    return cid


def _take(cid: str, purpose: str) -> tuple[bytes, int | None]:
    """Single use: a challenge that has been answered, rightly or wrongly, is gone."""
    entry = _challenges.pop(cid, None)
    if entry is None or entry[3] < time.monotonic() or entry[1] != purpose:
        raise HTTPException(400, "that request expired; start again")
    return entry[0], entry[2]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PasskeyOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None


class ChallengeOut(BaseModel):
    challenge_id: str
    # the options object the browser passes to navigator.credentials.create() / get()
    options: dict


class RegisterIn(BaseModel):
    challenge_id: str = Field(max_length=100)
    name: str = Field(min_length=1, max_length=60)
    # adding a way in is what an attacker with a stolen cookie would do first, so it asks for the password again
    password: str = Field(max_length=200)
    credential: dict


class PasskeyLoginIn(BaseModel):
    challenge_id: str = Field(max_length=100)
    credential: dict


def _out(row: Passkey) -> PasskeyOut:
    return PasskeyOut(id=row.id, name=row.name, created_at=row.created_at, last_used_at=row.last_used_at)


def _require_accounts() -> None:
    if not auth.operator_mode():
        raise HTTPException(400, "passkeys belong to accounts, which are off in local development")


@router.get("", response_model=list[PasskeyOut])
async def list_passkeys(who: auth.Who = Depends(auth.require_session), db: AsyncSession = Depends(get_session)) -> list[PasskeyOut]:
    rows = await db.scalars(select(Passkey).where(Passkey.operator_id == who.operator_id).order_by(Passkey.id))
    return [_out(r) for r in rows]


@router.post("/register/options", response_model=ChallengeOut)
async def register_options(who: auth.Who = Depends(auth.require_session), db: AsyncSession = Depends(get_session)) -> ChallengeOut:
    _require_accounts()
    op = await db.get(Operator, who.operator_id)
    existing = list(await db.scalars(select(Passkey).where(Passkey.operator_id == op.id)))
    if len(existing) >= MAX_PER_OPERATOR:
        raise HTTPException(409, f"{MAX_PER_OPERATOR} passkeys is the limit: remove one you no longer use")
    options = generate_registration_options(
        rp_id=rp_id(),
        rp_name=RP_NAME,
        user_id=str(op.id).encode(),
        user_name=op.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED
        ),
        # the browser refuses to register the same authenticator twice
        exclude_credentials=[PublicKeyCredentialDescriptor(id=p.credential_id) for p in existing],
    )
    return ChallengeOut(challenge_id=_remember(options.challenge, "register", op.id), options=json.loads(options_to_json(options)))


@router.post("/register", response_model=PasskeyOut, status_code=201)
async def register(
    body: RegisterIn, request: Request, who: auth.Who = Depends(auth.require_session), db: AsyncSession = Depends(get_session)
) -> PasskeyOut:
    _require_accounts()
    challenge, operator_id = _take(body.challenge_id, "register")
    if operator_id != who.operator_id:
        raise HTTPException(400, "that request expired; start again")
    op = await db.get(Operator, who.operator_id)
    ip = auth.client_ip(request)
    # the same lock-out that guards the sign-in form guards this, so a stolen cookie cannot be used to guess the password
    if not await auth.check_password_step_up(db, op, body.password, ip):
        raise HTTPException(401, "wrong password")
    try:
        verified = verify_registration_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=origins(),
            require_user_verification=True,
        )
    except (InvalidRegistrationResponse, InvalidJSONStructure, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, f"that passkey could not be verified: {exc}") from exc
    if await db.scalar(select(Passkey.id).where(Passkey.credential_id == verified.credential_id)):
        raise HTTPException(409, "that passkey is already registered")
    row = Passkey(
        operator_id=op.id, name=body.name.strip() or "passkey", credential_id=verified.credential_id,
        public_key=verified.credential_public_key, sign_count=verified.sign_count,
    )
    db.add(row)
    audit.record_login(db, "passkey_added", row.name, actor=op.username)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{passkey_id}")
async def remove(passkey_id: int, who: auth.Who = Depends(auth.require_session), db: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    # your own only: an owner removing someone else's lost phone is a job for disabling the account
    row = await db.get(Passkey, passkey_id)
    if row is None or row.operator_id != who.operator_id:
        raise HTTPException(404, "unknown passkey")
    audit.record_login(db, "passkey_removed", row.name, actor=who.name)
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@public_router.post("/options", response_model=ChallengeOut)
async def login_options() -> ChallengeOut:
    _require_accounts()
    # no allowed credentials: the authenticator offers the passkeys it holds for this site, so no username is asked for
    options = generate_authentication_options(rp_id=rp_id(), user_verification=UserVerificationRequirement.REQUIRED)
    return ChallengeOut(challenge_id=_remember(options.challenge, "login", None), options=json.loads(options_to_json(options)))


@public_router.post("/login")
async def login(body: PasskeyLoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    _require_accounts()
    ip = auth.client_ip(request)
    window = auth._ip_failures[ip]
    if auth._recent(window, time.monotonic()) >= auth.IP_FAIL_LIMIT:
        raise HTTPException(429, "too many failed attempts; wait a few minutes")
    challenge, _ = _take(body.challenge_id, "login")

    async def refuse() -> HTTPException:
        window.append(time.monotonic())
        await asyncio.sleep(0.5)
        return HTTPException(401, "that passkey was not accepted")

    try:
        raw_id = base64url_to_bytes(str(body.credential.get("rawId", "")))
    except Exception:  # noqa: BLE001 - any malformed id is simply not a passkey we know
        raise await refuse()
    row = await db.scalar(select(Passkey).where(Passkey.credential_id == raw_id).with_for_update())
    op = await db.get(Operator, row.operator_id) if row else None
    # a disabled account, or one not yet set up, is refused exactly like an unknown passkey
    if row is None or op is None or op.disabled_at is not None or op.setup_token_hash is not None:
        raise await refuse()
    try:
        verified = verify_authentication_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=origins(),
            credential_public_key=row.public_key,
            credential_current_sign_count=row.sign_count,
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, InvalidJSONStructure, KeyError, ValueError, TypeError):
        audit.record_login(db, "login_failed", f"passkey from {ip}", actor=op.username)
        await db.commit()
        raise await refuse()
    row.sign_count = verified.new_sign_count
    row.last_used_at = _now()
    await auth.open_session(db, request, response, op, via=f"passkey {row.name}")
    window.clear()
    return {"ok": True}
