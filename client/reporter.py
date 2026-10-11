"""Drop this into any FastAPI app you look after. This is the whole 'SDK'.

    from reporter import install_reporter
    install_reporter(app, url="https://hub.yourdomain/ingest/<key>/event",
                     secret="<site secret>")
"""

import hashlib
import hmac
import json
import logging
import traceback
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


log = logging.getLogger("nightjar")

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


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


def _frames(exc: BaseException) -> list[dict]:
    return [
        {
            "file": f.filename,
            "module": f.filename.rsplit("/", 1)[-1],
            "function": f.name,
            "lineno": f.lineno,
            "context": f.line,
        }
        for f in traceback.extract_tb(exc.__traceback__)
    ]


def install_reporter(
    app: FastAPI, url: str, secret: str, environment: str = "production"
) -> None:
    # An unusable hub is never silent: a mistyped address would otherwise look exactly like an app with no errors. It
    # says so once, here, in the app's own log, and then sends nothing. It never raises, because a reporter must not
    # break the app it reports on, and the app's own 500 is unchanged.
    refusal = hub_refusal(url)
    if refusal:
        try:
            log.error(refusal)
        except Exception:
            pass

    @app.exception_handler(Exception)
    async def _report(request: Request, exc: Exception) -> JSONResponse:
        body = json.dumps(
            {
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "frames": _frames(exc),
                "level": "error",
                "environment": environment,
                "request": {
                    "method": request.method,
                    "path": request.url.path,
                },
            }
        ).encode()

        signature = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()

        # Never let reporting break the app it is reporting on.
        try:
            if refusal:
                return JSONResponse({"detail": "internal server error"}, status_code=500)
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature": signature,
                    },
                )
        except Exception:
            pass

        return JSONResponse({"detail": "internal server error"}, status_code=500)
