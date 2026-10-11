"""The alert outbox as the console sees it.

An alert that could not be sent must never look like an alert that was not needed, so every notification
is listed here with what happened to it, and `undelivered` is the set the console must not let anyone
miss: rows that failed for good, and rows that have already failed once and are being retried.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Notification
from .schemas import NotificationOut

router = APIRouter(prefix="/api/notifications", tags=["alerts"])


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    status: Literal["all", "failed", "undelivered"] = "all",
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[NotificationOut]:
    """Newest first. `failed` is what gave up. `undelivered` is that plus anything that has already
    failed once and is still being retried, so a flaky provider shows before it exhausts its attempts."""
    query = select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    if status == "failed":
        query = query.where(Notification.status == "failed")
    elif status == "undelivered":
        query = query.where(
            or_(
                Notification.status == "failed",
                (Notification.status.in_(("pending", "sending"))) & (Notification.attempts > 0),
            )
        )
    return [NotificationOut.model_validate(n, from_attributes=True) for n in await session.scalars(query)]
