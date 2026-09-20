from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .api import acknowledged_ids, issue_out
from .db import get_session
from .models import Issue, IssueAction, Site
from .schemas import ActionIn, IssueOut, LogEntry

router = APIRouter(prefix="/api/issues", tags=["incidents"])

STATUS_AFTER = {"resolve": "resolved", "mute": "muted", "snooze": "snoozed", "reopen": "open"}


@router.post("/{issue_id}/actions", response_model=IssueOut)
async def act_on_issue(
    issue_id: int,
    body: ActionIn,
    session: AsyncSession = Depends(get_session),
) -> IssueOut:
    # Same lock ingest takes, so an action cannot interleave with an event
    # arriving for the same issue and leave status and counts disagreeing.
    issue = await session.scalar(select(Issue).where(Issue.id == issue_id).with_for_update())
    if issue is None:
        raise HTTPException(404, "unknown issue")

    detail = ""
    if body.action == "ack":
        if issue.status != "open":
            raise HTTPException(409, f"only an open issue can be acknowledged ({issue.status})")
    else:
        issue.status = STATUS_AFTER[body.action]
        issue.snoozed_until = None
        if body.action == "snooze":
            issue.snoozed_until = datetime.now(UTC) + timedelta(hours=body.hours)
            detail = f"{body.hours}h"

    session.add(IssueAction(issue_id=issue.id, action=body.action, detail=detail))
    await session.commit()

    site = await session.get(Site, issue.site_id)
    acked = await acknowledged_ids(session, [issue.id])
    return issue_out(issue, site.name, site.env, issue.id in acked)


ACTION_TEXT = {
    "ack": "Acknowledged",
    "resolve": "Resolved",
    "mute": "Muted. It keeps counting but will never alert again until unmuted",
    "snooze": "Snoozed",
    "reopen": "Reopened",
}


@router.get("/{issue_id}/log", response_model=list[LogEntry])
async def issue_log(
    issue_id: int, session: AsyncSession = Depends(get_session)
) -> list[LogEntry]:
    """What happened to this issue, oldest first. `system` rows are derived from
    the issue itself; `action` rows are things a person did. Alerts sent by the
    hub will join this log with chunk 2."""
    issue = await session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(404, "unknown issue")

    entries = [
        LogEntry(
            at=issue.first_seen,
            kind="system",
            text=f"First occurrence. Issue opened at {issue.severity}",
        )
    ]
    for a in await session.scalars(
        select(IssueAction)
        .where(IssueAction.issue_id == issue_id)
        .order_by(IssueAction.created_at, IssueAction.id)
    ):
        text = ACTION_TEXT[a.action] + (f" for {a.detail}" if a.detail else "")
        entries.append(LogEntry(at=a.created_at, kind="action", text=text))
    if issue.event_count > 1:
        entries.append(
            LogEntry(
                at=issue.last_seen,
                kind="system",
                text=f"Most recent of {issue.event_count:,} occurrences",
            )
        )
    entries.sort(key=lambda e: e.at)
    return entries
