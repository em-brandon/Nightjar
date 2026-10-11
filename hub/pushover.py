"""Pushover's acknowledgement callback.

A priority 2 push repeats on the phone until it is acknowledged, and when it is, Pushover POSTs here (it is
the `callback` field set in hub/notify.py). So there is no signed ack link to build: acknowledging the
alert on the phone acknowledges the incident, the same as the console's Acknowledge button, and stops
nothing else. It adds an `ack` to the issue's audit trail and stamps the notification.

The route is public, because Pushover cannot send the console token, and its callback carries no
signature. So the URL itself holds a secret (PUSHOVER_CALLBACK_TOKEN), compared in constant time. Without
that setting the route refuses everything, and a wrong token is refused the same way.
"""

import hmac
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session
from .models import Issue, IssueAction, Notification

log = logging.getLogger("nightjar.pushover")

router = APIRouter(tags=["pushover"])


@router.post("/pushover/ack/{token}")
async def acknowledged(token: str, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    expected = settings.pushover_callback_token
    # one answer for "not configured" and "wrong": the route says nothing about which
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "unauthorized")

    # Pushover sends a form. Parsed by hand so the hub needs no multipart dependency for one route.
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    receipt = form.get("receipt", "")
    if not receipt:
        return {"matched": False}

    note = await session.scalar(select(Notification).where(Notification.receipt == receipt).with_for_update())
    if note is None:
        # Some other receipt: answered 200 so Pushover does not keep calling back
        log.info("acknowledgement for a receipt this hub does not know")
        return {"matched": False}

    # Only an acknowledgement counts. The same callback also reports a receipt that expired unanswered.
    if form.get("acknowledged") != "1" or note.acked_at is not None:
        return {"matched": True}

    note.acked_at = datetime.now(UTC)
    # Same lock ingest and the console's actions take, so this cannot interleave with an event
    issue = await session.scalar(select(Issue).where(Issue.id == note.issue_id).with_for_update())
    if issue is not None and issue.status == "open":
        session.add(IssueAction(issue_id=issue.id, action="ack", detail="phone"))
    await session.commit()
    return {"matched": True}
