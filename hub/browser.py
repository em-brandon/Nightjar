"""Errors reported from a visitor's browser.

Every other ingest route proves who is calling with an HMAC over the body. A web page cannot
keep a secret: anything in its source is public. So this route trusts nothing about the caller
and instead BOUNDS what a caller can do:

  * the key only reports errors, and only for its own site: it opens no other route;
  * the Origin must be one the operator listed for that site;
  * the body is small, the fields are capped and the level is clamped (never fatal, so never P1);
  * calls are rate limited per key and per visitor;
  * a site may open only a few NEW issues an hour from browsers.

The Origin check stops other websites using a key lifted from a page. It does NOT stop someone with
curl, who can set any header; that is what the limits are for. Fingerprinting already collapses
repeats of one error. A flood of *different* errors is the case it cannot, hence the new-issue cap.
"""

import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import fingerprint as fp
from .config import settings
from .db import get_session
from .ingest import record_event
from .models import Issue, Site
from .schemas import BrowserEventIn, EventIn

router = APIRouter(prefix="/ingest/browser", tags=["browser"])

MAX_BODY = 16 * 1024
WINDOW_S = 60
HOUR_S = 3600
MAX_BUCKETS = 10_000  # bounds memory against a flood of distinct visitors
FIELD_CAP = 200


class _Window:
    """Events in the last `span` seconds, per bucket. In memory and per process: this is a brake
    against abuse, not an accounting system, so losing it on a restart is acceptable."""

    def __init__(self, span: float):
        self.span = span
        self.hits: dict[object, deque[float]] = {}

    def _live(self, bucket: object, now: float) -> deque[float]:
        q = self.hits.get(bucket)
        if q is None:
            if len(self.hits) >= MAX_BUCKETS:
                self.hits.clear()  # a flood of distinct visitors: forget them all rather than grow
            q = self.hits[bucket] = deque()
        while q and now - q[0] > self.span:
            q.popleft()
        return q

    def count(self, bucket: object) -> int:
        return len(self._live(bucket, time.monotonic()))

    def add(self, bucket: object) -> None:
        now = time.monotonic()
        self._live(bucket, now).append(now)


_per_key = _Window(WINDOW_S)
_per_visitor = _Window(WINDOW_S)
_new_issues = _Window(HOUR_S)


def reset_limits() -> None:
    """For tests."""
    for w in (_per_key, _per_visitor, _new_issues):
        w.hits.clear()


def _visitor(request: Request) -> str:
    # Behind Caddy (and Cloudflare) the socket address is the proxy, so the real address arrives in
    # a header. A caller can fake it to dodge the per-visitor limit; the per-key limit and the
    # new-issue cap still hold, so faking it buys nothing beyond the key's own ceiling.
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _cut(value: str | None) -> str | None:
    return value[:FIELD_CAP] if value else value


def _page(url: str | None) -> str | None:
    """Scheme, host and path only. A query string or fragment can carry a token or a person's
    details, and the issue does not need them."""
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:500]


def _allow_headers(origin: str) -> dict[str, str]:
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}


async def _site_for(session: AsyncSession, public_key: str, request: Request) -> tuple[Site, str]:
    site = await session.scalar(select(Site).where(Site.public_key == public_key))
    if site is None:
        raise HTTPException(404, "unknown key")
    origin = request.headers.get("origin", "")
    if origin not in (site.allowed_origins or []):
        # A browser always sends Origin on a cross-origin POST, so a missing one is not a browser.
        raise HTTPException(403, "origin not allowed")
    return site, origin


@router.options("/{public_key}", status_code=204)
async def preflight(
    public_key: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    # The reporter sends text/plain, which needs no preflight. This is for anyone who sends JSON.
    _, origin = await _site_for(session, public_key, request)
    return Response(
        status_code=204,
        headers={
            **_allow_headers(origin),
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Max-Age": "600",
        },
    )


@router.post("/{public_key}", status_code=202)
async def browser_event(
    public_key: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(413, "report too large")
    site, origin = await _site_for(session, public_key, request)

    visitor = (public_key, _visitor(request))
    if (
        _per_key.count(public_key) >= settings.browser_rate_per_key_min
        or _per_visitor.count(visitor) >= settings.browser_rate_per_ip_min
    ):
        raise HTTPException(429, "slow down", headers={"Retry-After": "60"})
    _per_key.add(public_key)
    _per_visitor.add(visitor)

    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "report too large")
    try:
        body = BrowserEventIn.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            422, exc.errors(include_url=False, include_context=False, include_input=False)
        ) from exc

    frames = []
    for f in body.frames:
        file = (f.file or "").split("?", 1)[0].split("#", 1)[0]
        frame = {
            "module": _cut(f.module),
            "file": _cut(file) or None,
            "function": _cut(f.function),
            "lineno": f.lineno,
        }
        frames.append({k: v for k, v in frame.items() if v is not None})
    event = EventIn(
        exception_type=body.exception_type,
        message=body.message,
        frames=frames,
        level="warning" if body.level == "warning" else "error",
        environment=body.environment,
        release=body.release,
        request={"source": "browser", "url": _page(body.page)},
    )

    # The cap on NEW issues. Known fingerprints always count, so an error that is happening a
    # thousand times is still counted; only an unfamiliar one can be turned away.
    digest = fp.compute(site.id, event.exception_type, event.message, frames)
    known = await session.scalar(
        select(Issue.id).where(Issue.site_id == site.id, Issue.fingerprint == digest)
    )
    headers = _allow_headers(origin)
    if known is None and _new_issues.count(site.id) >= settings.browser_new_issues_per_hour:
        return Response(status_code=202, headers=headers)  # accepted in form, dropped in fact

    result = await record_event(session, site, event)
    if result.state_change == "new":
        _new_issues.add(site.id)
    return Response(status_code=202, headers=headers)
