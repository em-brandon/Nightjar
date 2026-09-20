from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Issue, Site
from .schemas import IssueOut, SiteHealthOut

# Read-only, for the console. Everything the console shows is derived from
# issues; there is no separate health table to drift out of sync with them.
router = APIRouter(prefix="/api", tags=["console"])

ISSUE_LIMIT = 100


@router.get("/issues", response_model=list[IssueOut])
async def list_issues(session: AsyncSession = Depends(get_session)) -> list[IssueOut]:
    # P1 before P2 before P3, then most recently active. "P1" < "P2" < "P3"
    # sorts correctly as text, but an explicit case survives a new severity.
    rank = case((Issue.severity == "P1", 1), (Issue.severity == "P2", 2), else_=3)
    rows = await session.execute(
        select(Issue, Site.name, Site.env)
        .join(Site, Issue.site_id == Site.id)
        .where(Issue.status == "open")
        .order_by(rank, Issue.last_seen.desc())
        .limit(ISSUE_LIMIT)
    )
    return [
        IssueOut(
            id=issue.id,
            site=site_name,
            env=env,
            severity=issue.severity,
            title=issue.title,
            culprit=issue.culprit,
            fingerprint=issue.fingerprint,
            event_count=issue.event_count,
            first_seen=issue.first_seen,
            last_seen=issue.last_seen,
        )
        for issue, site_name, env in rows
    ]


@router.get("/sites/health", response_model=list[SiteHealthOut])
async def sites_health(session: AsyncSession = Depends(get_session)) -> list[SiteHealthOut]:
    is_open = Issue.status == "open"

    def open_count(severity: str):
        return func.count(Issue.id).filter(is_open, Issue.severity == severity)

    # Outer join so a site with no issues at all still appears, as "up".
    rows = await session.execute(
        select(
            Site.id,
            Site.name,
            Site.env,
            open_count("P1"),
            open_count("P2"),
            open_count("P3"),
            func.max(Issue.last_seen),
        )
        .select_from(Site)
        .outerjoin(Issue, Issue.site_id == Site.id)
        .group_by(Site.id)
        .order_by(Site.name)
    )
    out = []
    for site_id, name, env, p1, p2, p3, last_event in rows:
        state = "down" if p1 else "warn" if p2 else "up"
        out.append(
            SiteHealthOut(
                id=site_id,
                name=name,
                env=env,
                state=state,
                open_p1=p1,
                open_p2=p2,
                open_p3=p3,
                last_event=last_event,
            )
        )
    return out
