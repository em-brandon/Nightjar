"""Console authentication. Guards everything under /api (the console's feeds and every write). It does NOT guard
/ingest, which authenticates by per-site key plus HMAC, or /healthz.

Two ways in, always:
  * `Authorization: Bearer <CONSOLE_TOKEN>`, for scripts and the wall display.
  * a session cookie, set by POST /api/auth/login. HttpOnly, so page JavaScript (and anything injected into it) never
    sees it; SameSite=Strict, so another site cannot make the browser send it.

What the cookie means depends on the mode (operator_mode()):
  * account mode (production): you sign in with a username, a password and a TOTP code, and get a random session that is
    stored hashed in the database, expires, and can be revoked on its own. The shared token cannot sign in here.
  * token mode (local development): you type CONSOLE_TOKEN and the cookie is derived from it. Simple, and not for
    anything on the internet.

With CONSOLE_TOKEN unset, auth is off. That is the local-development default, and the hub refuses to start that way when
VERIFY_SIGNATURES is on (production).
"""

import asyncio
import hashlib
import hmac
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, credentials
from .config import settings
from .db import SessionLocal, get_session
from .derive import MIN_MASTER_LEN
from .models import ConsoleSession, Operator
from .sources import client_ip

log = logging.getLogger("nightjar.auth")

COOKIE = "nj_session"  # token mode: derived from CONSOLE_TOKEN
SESSION_COOKIE = "nj_sid"  # account mode: a random token whose hash is in console_sessions
COOKIE_MAX_AGE = 30 * 24 * 3600  # token mode only. Account mode follows settings.session_absolute_days
MIN_TOKEN_LEN = 16

# Failed token logins in the last minute, across everyone. One operator uses this, so a
# global limit is enough to make guessing the secret impractical.
FAIL_WINDOW_S = 60
FAIL_LIMIT = 10
_failures: deque[float] = deque()

# Account mode keeps its own failure counts, per account and per address. In memory on purpose: the hub is one process
# (CLAUDE.md), and a restart only forgives a guesser. The per-account limit locks the account for everyone for the
# window, which lets a stranger stall a sign-in for 15 minutes but never get in; the bearer token is unaffected, so you
# are not locked out of your own hub.
ACCOUNT_WINDOW_S = 15 * 60
ACCOUNT_FAIL_LIMIT = 10
IP_FAIL_LIMIT = 20
_account_failures: dict[str, deque[float]] = defaultdict(deque)
_ip_failures: dict[str, deque[float]] = defaultdict(deque)
# touch last_seen_at at most this often, so a page polling every 30 s is not a database write each time
LAST_SEEN_EVERY_S = 60

router = APIRouter(prefix="/api/auth", tags=["auth"])


def enabled() -> bool:
    return bool(settings.console_token)


def operator_mode() -> bool:
    """Sign in with an account (production) rather than the shared token (local development)."""
    if not enabled():
        return False
    return settings.verify_signatures if settings.operator_login is None else settings.operator_login


def check_config() -> None:
    """Called at startup. Refuse to run in production with the console open."""
    token = settings.console_token
    if token and len(token) < MIN_TOKEN_LEN:
        raise RuntimeError(
            f"CONSOLE_TOKEN is too short ({len(token)} characters). Use at least "
            f"{MIN_TOKEN_LEN}, e.g. `openssl rand -base64 32`."
        )
    if settings.verify_signatures and not token:
        raise RuntimeError(
            "Refusing to start: VERIFY_SIGNATURES is on (production) but CONSOLE_TOKEN "
            "is not set, so the console API, including every write, would be open to "
            "anyone who can reach it. Set CONSOLE_TOKEN in the environment, or mount it as the file "
            "/run/secrets/console_token."
        )
    master = settings.secret_master_key
    if master and len(master) < MIN_MASTER_LEN:
        raise RuntimeError(
            f"SECRET_MASTER_KEY is too short ({len(master)} characters). Use at least "
            f"{MIN_MASTER_LEN}, e.g. `openssl rand -base64 32`."
        )
    if settings.verify_signatures and not master:
        raise RuntimeError(
            "Refusing to start: VERIFY_SIGNATURES is on (production) but SECRET_MASTER_KEY is not set, so each "
            "site's ingest secret would have to sit readable in the database. Set SECRET_MASTER_KEY in the "
            "environment, or mount it as the file /run/secrets/secret_master_key. Keep a copy somewhere other than "
            "this server: lose it and every site needs a new secret."
        )
    if operator_mode() and not master:
        raise RuntimeError(
            "Refusing to start: account sign-in is on but SECRET_MASTER_KEY is not set, and the two-factor code is "
            "derived from it. Set SECRET_MASTER_KEY, or set OPERATOR_LOGIN=false for local development."
        )
    if not token:
        log.warning("console authentication is OFF (CONSOLE_TOKEN is not set). Fine for local development only.")


def _session_value() -> str:
    # Token mode. Derived from the token, so changing CONSOLE_TOKEN signs every session out.
    return hmac.new(settings.console_token.encode(), b"nightjar-console-session-v1", hashlib.sha256).hexdigest()


def _matches(candidate: str, expected: str) -> bool:
    # constant time, so response timing does not leak how much of a guess was right
    return hmac.compare_digest(candidate.encode(), expected.encode())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Who:
    """What authenticated a request. session_id is set only for an account session."""

    def __init__(self, kind: str, session_id: int | None = None, operator_id: int | None = None):
        self.kind = kind  # "bearer" | "token-cookie" | "session"
        self.session_id = session_id
        self.operator_id = operator_id


async def _session_who(request: Request, db: AsyncSession) -> Who | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw or len(raw) > 200:
        return None
    row = await db.scalar(select(ConsoleSession).where(ConsoleSession.token_hash == credentials.hash_token(raw)))
    if row is None or row.revoked_at is not None:
        return None
    now = _now()
    if row.expires_at <= now or row.last_seen_at < now - timedelta(days=settings.session_idle_days):
        return None
    if (now - row.last_seen_at).total_seconds() > LAST_SEEN_EVERY_S:
        row.last_seen_at = now
        await db.commit()
    return Who("session", row.id, row.operator_id)


async def authenticate(request: Request, db: AsyncSession) -> Who | None:
    if not enabled():
        return Who("bearer")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and _matches(header[7:].strip(), settings.console_token):
        return Who("bearer")
    if operator_mode():
        return await _session_who(request, db)
    cookie = request.cookies.get(COOKIE)
    if cookie and _matches(cookie, _session_value()):
        return Who("token-cookie")
    return None


async def require_console(request: Request, db: AsyncSession = Depends(get_session)) -> None:
    """Dependency for every console route."""
    who = await authenticate(request, db)
    if who is None:
        raise HTTPException(401, "authentication required")
    request.state.who = who


async def require_session(request: Request, db: AsyncSession = Depends(get_session)) -> Who:
    """For routes about the signed-in account itself: a bearer token has no account behind it."""
    await require_console(request, db)
    who: Who = request.state.who
    if who.session_id is None:
        raise HTTPException(403, "this needs a signed-in session, not the bearer token")
    return who


class LoginIn(BaseModel):
    # Token mode sends `token`. Account mode sends the other three. `code` is a 6-digit authenticator code or a recovery code.
    token: str = Field(default="", max_length=200)
    username: str = Field(default="", max_length=60)
    password: str = Field(default="", max_length=200)
    code: str = Field(default="", max_length=32)


class StatusOut(BaseModel):
    required: bool
    authenticated: bool
    # True when the sign-in form should ask for a username, password and code instead of the console token
    account: bool = False


@router.get("/status", response_model=StatusOut)
async def status(request: Request, db: AsyncSession = Depends(get_session)) -> StatusOut:
    return StatusOut(
        required=enabled(),
        authenticated=await authenticate(request, db) is not None,
        account=operator_mode(),
    )


def _recent(window: deque[float], now: float) -> int:
    while window and now - window[0] > ACCOUNT_WINDOW_S:
        window.popleft()
    return len(window)


def _cookie_secure(request: Request) -> bool:
    # Behind Caddy the request arrives as plain http, so trust its forwarded proto.
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


@router.post("/login")
async def login(
    body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> dict[str, bool]:
    if not enabled():
        return {"ok": True}
    if operator_mode():
        return await _account_login(body, request, response, db)

    now = time.monotonic()
    while _failures and now - _failures[0] > FAIL_WINDOW_S:
        _failures.popleft()
    if len(_failures) >= FAIL_LIMIT:
        raise HTTPException(429, "too many failed attempts; wait a minute")

    if not _matches(body.token, settings.console_token):
        _failures.append(now)
        await asyncio.sleep(0.5)  # a small fixed cost per wrong guess
        raise HTTPException(401, "wrong token")

    response.set_cookie(
        COOKIE,
        _session_value(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(request),
        path="/",
    )
    return {"ok": True}


async def _account_login(body: LoginIn, request: Request, response: Response, db: AsyncSession) -> dict[str, bool]:
    username = body.username.strip().lower()
    ip = client_ip(request)
    now = time.monotonic()
    ip_window, user_window = _ip_failures[ip], _account_failures[username]
    if _recent(ip_window, now) >= IP_FAIL_LIMIT or _recent(user_window, now) >= ACCOUNT_FAIL_LIMIT:
        raise HTTPException(429, "too many failed attempts; wait a few minutes")

    # The same work and the same answer whether the username exists, the password is wrong or the code is wrong.
    op = await db.scalar(select(Operator).where(Operator.username == username).with_for_update()) if username else None
    password_ok = await asyncio.to_thread(
        credentials.verify_password, body.password, op.password_hash if op else credentials.DUMMY_HASH
    )
    code_ok = False
    if op is not None and password_ok and settings.secret_master_key:
        code_ok = await _spend_code(db, op, body.code)

    if not (op is not None and password_ok and code_ok):
        ip_window.append(now)
        if op is not None:
            user_window.append(now)
            audit.record_login(db, "login_failed", f"from {ip}", actor=op.username)
            await db.commit()
        else:
            await db.rollback()
        await asyncio.sleep(0.5)
        raise HTTPException(401, "wrong username, password or code")

    token, token_hash = credentials.new_session_token()
    created = _now()
    db.add(
        ConsoleSession(
            token_hash=token_hash,
            operator_id=op.id,
            created_at=created,
            last_seen_at=created,
            expires_at=created + timedelta(days=settings.session_absolute_days),
            ip=ip,
            user_agent=request.headers.get("user-agent", "")[:200],
        )
    )
    # housekeeping: a session that ended over a week ago has nothing left to show
    await db.execute(
        delete(ConsoleSession).where(
            (ConsoleSession.expires_at < created - timedelta(days=7))
            | (ConsoleSession.revoked_at < created - timedelta(days=7))
        )
    )
    audit.record_login(db, "login", f"from {ip}", actor=op.username)
    await db.commit()
    ip_window.clear()
    user_window.clear()

    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_absolute_days * 24 * 3600,
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(request),
        path="/",
    )
    return {"ok": True}


async def _spend_code(db: AsyncSession, op: Operator, code: str) -> bool:
    """Accept an authenticator code, or a recovery code (which is then used up). The operator row is locked by the
    caller, so two simultaneous requests cannot both spend the same code."""
    code = code.strip()
    secret = credentials.totp_secret(settings.secret_master_key, op.id, op.totp_version)
    step = credentials.totp_match(secret, code, op.totp_last_step)
    if step is not None:
        op.totp_last_step = step
        return True
    left = credentials.use_recovery(code, list(op.recovery_hashes or []))
    if left is not None:
        op.recovery_hashes = left
        audit.record_login(db, "recovery_code_used", f"{len(left)} left", actor=op.username)
        return True
    return False


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw and len(raw) <= 200:
        await db.execute(
            update(ConsoleSession)
            .where(ConsoleSession.token_hash == credentials.hash_token(raw), ConsoleSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        await db.commit()
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


class SessionOut(BaseModel):
    id: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str
    user_agent: str
    current: bool


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    who: Who = Depends(require_session), db: AsyncSession = Depends(get_session)
) -> list[SessionOut]:
    now = _now()
    rows = await db.scalars(
        select(ConsoleSession)
        .where(
            ConsoleSession.operator_id == who.operator_id,
            ConsoleSession.revoked_at.is_(None),
            ConsoleSession.expires_at > now,
            ConsoleSession.last_seen_at > now - timedelta(days=settings.session_idle_days),
        )
        .order_by(ConsoleSession.last_seen_at.desc())
    )
    return [
        SessionOut(
            id=r.id,
            created_at=r.created_at,
            last_seen_at=r.last_seen_at,
            expires_at=r.expires_at,
            ip=r.ip,
            user_agent=r.user_agent,
            current=r.id == who.session_id,
        )
        for r in rows
    ]


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: int, who: Who = Depends(require_session), db: AsyncSession = Depends(get_session)
) -> dict[str, bool]:
    row = await db.get(ConsoleSession, session_id)
    if row is None or row.operator_id != who.operator_id:
        raise HTTPException(404, "unknown session")
    if row.revoked_at is None:
        row.revoked_at = _now()
        audit.record_login(db, "session_revoked", f"session {row.id} from {row.ip}")
        await db.commit()
    return {"ok": True}


@router.post("/sessions/revoke-others")
async def revoke_others(
    who: Who = Depends(require_session), db: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    result = await db.execute(
        update(ConsoleSession)
        .where(
            ConsoleSession.operator_id == who.operator_id,
            ConsoleSession.id != who.session_id,
            ConsoleSession.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )
    audit.record_login(db, "sessions_revoked", f"{result.rowcount} other session(s)")
    await db.commit()
    return {"revoked": result.rowcount}


async def check_operators() -> None:
    """Startup note, not a refusal: a hub with no account yet is still reachable with the bearer token, and refusing
    to start would make a fresh deploy impossible to set up."""
    if not operator_mode():
        return
    async with SessionLocal() as db:
        count = await db.scalar(select(func.count()).select_from(Operator))
    if not count:
        log.warning(
            "account sign-in is on but there is no account yet, so nobody can sign in to the console. Create one "
            "with: docker compose exec hub python -m hub.operator_cli create"
        )
