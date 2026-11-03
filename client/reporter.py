"""Drop this into any FastAPI app you look after. This is the whole 'SDK'.

    from reporter import install_reporter
    install_reporter(app, url="https://hub.yourdomain/ingest/<key>/event",
                     secret="<site secret>")
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import traceback
from collections import deque
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


log = logging.getLogger("nightjar")

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# A function, not time.monotonic directly, so a test can move the clock without touching everyone else's.
_clock = time.monotonic


def hub_refusal(url: str) -> str | None:
    """Whether this address may be used, and if not, why. https is always fine. Plain http is fine only for a machine
    talking to itself, which development needs: anywhere else it would send error text, readable, across a network.
    Returns None when the address is fine, or one line saying what is wrong. It never includes the whole address, which
    could carry a credential."""
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    except ValueError:
        scheme, host = "", ""
    if not scheme or not host:
        return "Nightjar: the hub address is not a valid URL, so no errors will be reported. Use https://your-hub/ingest/<key>/event."
    if scheme == "https":
        return None
    if scheme == "http" and (host in _LOOPBACK or host.endswith(".localhost")):
        return None
    return (
        f"Nightjar: refusing to report errors to {scheme}://{host}: plain http would send them readable across the "
        "network. Use an https:// hub address (http:// is only accepted for localhost). No errors will be reported."
    )


_SCRUBBERS = [
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 [redacted]"),
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@"), "[redacted]@"),
    (
        re.compile(r"""\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization)(["']?\s*[=:]\s*["']?)[^\s"'&,;]+""", re.I),
        r"\1\2[redacted]",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
]


def scrub(text: str) -> str:
    """Take the obvious secrets out of text before it leaves the server: passwords and tokens as key=value, credentials in
    URLs, bearer tokens and email addresses. Best effort, not a guarantee, so do not put secrets in exception messages."""
    for pattern, replacement in _SCRUBBERS:
        text = pattern.sub(replacement, text)
    return text


def _relative(path: str) -> str:
    """A path without the server's own directory layout (home directories, account names)."""
    path = path.replace("\\", "/")
    base = os.getcwd().replace("\\", "/").rstrip("/") + "/"
    return path[len(base):] if path.startswith(base) else path


def _frames(exc: BaseException) -> list[dict]:
    return [
        {
            "file": _relative(f.filename),
            "module": f.filename.replace("\\", "/").rsplit("/", 1)[-1],
            "function": f.name,
            "lineno": f.lineno,
            "context": scrub(f.line or ""),  # a line of source can hold a literal credential
        }
        for f in traceback.extract_tb(exc.__traceback__)
    ]


def install_reporter(
    app: FastAPI,
    url: str,
    secret: str,
    environment: str = "production",
    max_per_minute: int = 60,
    max_in_flight: int = 5,
) -> None:
    """Report every unhandled exception to the hub, then answer exactly as the app would have.

    The report is sent in the background, so a slow or dead hub never makes an error response slower. Two caps keep an
    error storm from becoming a connection storm: at most `max_per_minute` reports a minute and `max_in_flight` sends
    at once, and the rest are dropped. That is deliberate: the hub already collapses repeats into one issue, and a
    dropped report costs far less than a pile of requests waiting on a hub that is down."""
    # An unusable hub is never silent: a mistyped address would otherwise look exactly like an app with no errors. It
    # says so once, here, in the app's own log, and then sends nothing. It never raises, because a reporter must not
    # break the app it reports on, and the app's own 500 is unchanged.
    refusal = hub_refusal(url)
    if refusal:
        try:
            log.error(refusal)
        except Exception:
            pass

    in_flight: set[asyncio.Task] = set()  # also keeps each task referenced, or it could be collected mid-send
    sent_at: deque[float] = deque()
    app.state.nightjar_in_flight = in_flight  # read-only, so a test or a shutdown hook can wait for sends to finish

    def admit() -> bool:
        now = _clock()
        while sent_at and now - sent_at[0] >= 60:
            sent_at.popleft()
        if len(sent_at) >= max_per_minute or len(in_flight) >= max_in_flight:
            return False
        sent_at.append(now)
        return True

    async def send(body: bytes, timestamp: str, signature: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Timestamp": timestamp,
                        "X-Hub-Signature": signature,
                    },
                )
        except Exception:
            pass  # a failed report is not the app's problem

    def report(request: Request, exc: Exception) -> None:
        if refusal or not admit():
            return
        body = json.dumps(
            {
                "exception_type": type(exc).__name__,
                "message": scrub(str(exc)),
                "frames": _frames(exc),
                "level": "error",
                "environment": environment,
                "request": {
                    "method": request.method,
                    "path": request.url.path,
                },
            }
        ).encode()
        # The timestamp is signed with the body, so a captured report cannot be replayed later (hub/ingest.py).
        timestamp = str(int(time.time()))
        signature = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        task = asyncio.get_running_loop().create_task(send(body, timestamp, signature))
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)

    @app.exception_handler(Exception)
    async def _report(request: Request, exc: Exception) -> JSONResponse:
        # Never let reporting break the app it is reporting on, even if building the report fails (an exception whose
        # str() raises, say).
        try:
            report(request, exc)
        except Exception:
            pass
        return JSONResponse({"detail": "internal server error"}, status_code=500)
