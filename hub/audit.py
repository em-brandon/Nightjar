from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import AuditLog, Site

# "Who rotated that secret, and when?" Issue actions already answer this for incidents; this
# answers it for sites and their credentials (docs/DATA-PROTECTION.md, item 7).
#
# Two rules. It never holds a secret value, only the fact that one changed. And nothing here
# edits or deletes a row: the only route is a read, so the trail is append-only from the hub's side.
router = APIRouter(prefix="/api/sites", tags=["audit"])


def _where(url: str | None) -> str:
    """A site's address without any userinfo or query string, either of which can carry a credential."""
    if not url:
        return "none"
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}"[:200]


def record(session: AsyncSession, site: Site, action: str, detail: str = "") -> None:
    """Add a row to the caller's transaction. It commits or rolls back with the change it describes,
    so the trail can neither miss a change nor describe one that did not happen."""
    session.add(AuditLog(site_id=site.id, site_name=site.name, action=action, detail=detail[:300]))


def record_login(session: AsyncSession, action: str, detail: str = "", actor: str = "console") -> None:
    """Sign-in events live in the same trail but belong to no site. Never put a password, code or token in `detail`."""
    session.add(AuditLog(site_id=None, site_name="", action=action, detail=detail[:300], actor=actor[:40]))


class AuditOut(BaseModel):
    id: int
    action: str
    detail: str
    actor: str
    created_at: datetime


@router.get("/{site_id:int}/audit", response_model=list[AuditOut])
async def site_audit(site_id: int, session: AsyncSession = Depends(get_session)) -> list[AuditOut]:
    if await session.get(Site, site_id) is None:
        raise HTTPException(404, "unknown site")
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.site_id == site_id).order_by(AuditLog.id.desc()).limit(200)
    )
    return [AuditOut.model_validate(r, from_attributes=True) for r in rows]
