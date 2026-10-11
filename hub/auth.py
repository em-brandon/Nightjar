"""Console authentication: one shared secret, CONSOLE_TOKEN.

Multi-user auth is out of scope, and a single shared secret is not. It guards
everything under /api (the console's feeds and every write). It does NOT guard
/ingest, which authenticates by per-site key plus HMAC, or /healthz.

Two ways in:
  * a session cookie, set by POST /api/auth/login. HttpOnly, so page JavaScript
    (and anything injected into it) never sees the secret; SameSite=Strict, so
    another site cannot make the browser send it.
  * `Authorization: Bearer <token>`, for scripts.

With CONSOLE_TOKEN unset, auth is off. That is the local-development default, and
the hub refuses to start that way when VERIFY_SIGNATURES is on (production).
"""

import asyncio
import hashlib
import hmac
import logging
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import settings

log = logging.getLogger("nightjar.auth")

COOKIE = "nj_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # a wall display logs in once and stays logged in
MIN_TOKEN_LEN = 16

# Failed logins in the last minute, across everyone. One operator uses this, so a
# global limit is enough to make guessing the secret impractical.
FAIL_WINDOW_S = 60
FAIL_LIMIT = 10
_failures: deque[float] = deque()

router = APIRouter(prefix="/api/auth", tags=["auth"])


def enabled() -> bool:
    return bool(settings.console_token)


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
    if not token:
        log.warning("console authentication is OFF (CONSOLE_TOKEN is not set). Fine for local development only.")


def _session_value() -> str:
    # Derived from the token, so changing CONSOLE_TOKEN signs every session out.
    return hmac.new(settings.console_token.encode(), b"nightjar-console-session-v1", hashlib.sha256).hexdigest()


def _matches(candidate: str, expected: str) -> bool:
    # constant time, so response timing does not leak how much of a guess was right
    return hmac.compare_digest(candidate.encode(), expected.encode())


def is_authenticated(request: Request) -> bool:
    if not enabled():
        return True
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and _matches(header[7:].strip(), settings.console_token):
        return True
    cookie = request.cookies.get(COOKIE)
    return bool(cookie) and _matches(cookie, _session_value())


async def require_console(request: Request) -> None:
    """Dependency for every console route."""
    if not is_authenticated(request):
        raise HTTPException(401, "authentication required")


class LoginIn(BaseModel):
    token: str = Field(max_length=200)


@router.get("/status")
async def status(request: Request) -> dict[str, bool]:
    return {"required": enabled(), "authenticated": is_authenticated(request)}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, bool]:
    if not enabled():
        return {"ok": True}

    now = time.monotonic()
    while _failures and now - _failures[0] > FAIL_WINDOW_S:
        _failures.popleft()
    if len(_failures) >= FAIL_LIMIT:
        raise HTTPException(429, "too many failed attempts; wait a minute")

    if not _matches(body.token, settings.console_token):
        _failures.append(now)
        await asyncio.sleep(0.5)  # a small fixed cost per wrong guess
        raise HTTPException(401, "wrong token")

    # Behind Caddy the request arrives as plain http, so trust its forwarded proto.
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE,
        _session_value(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
