"""Drop this into any FastAPI app you look after. This is the whole 'SDK'.

    from reporter import install_reporter
    install_reporter(app, url="https://hub.yourdomain/ingest/<key>/event",
                     secret="<site secret>")
"""

import hashlib
import hmac
import json
import traceback

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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
