from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import Site, Ticket
from .schemas import TicketIn, TicketOut, TicketPatch

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

TICKET_LIMIT = 200


def _out(t: Ticket, now: datetime) -> TicketOut:
    return TicketOut(
        id=t.id,
        kind=t.kind,
        subject=t.subject,
        sender=t.sender,
        site=t.site.name if t.site else None,
        status=t.status,
        created_at=t.created_at,
        due_at=t.due_at,
        closed_at=t.closed_at,
        overdue=t.status == "open" and t.due_at is not None and t.due_at < now,
    )


@router.get("", response_model=list[TicketOut])
async def list_tickets(
    status: Literal["open", "closed", "all"] = "open",
    session: AsyncSession = Depends(get_session),
) -> list[TicketOut]:
    # Eager-load the site: an async session cannot lazy-load it while we serialise.
    query = (
        select(Ticket)
        .options(selectinload(Ticket.site))
        .order_by(Ticket.created_at)
        .limit(TICKET_LIMIT)
    )
    if status != "all":
        query = query.where(Ticket.status == status)
    if status == "closed":
        query = query.order_by(None).order_by(Ticket.closed_at.desc())
    now = datetime.now(UTC)
    # oldest open first: the one waiting longest is the one you owe an answer
    return [_out(t, now) for t in await session.scalars(query)]


@router.post("", response_model=TicketOut, status_code=201)
async def create_ticket(
    body: TicketIn, session: AsyncSession = Depends(get_session)
) -> TicketOut:
    site_id = None
    if body.site:
        site_id = await session.scalar(select(Site.id).where(Site.name == body.site))
        if site_id is None:
            raise HTTPException(404, f"unknown site: {body.site}")
    ticket = Ticket(
        site_id=site_id,
        kind=body.kind,
        subject=body.subject,
        sender=body.sender,
        due_at=body.due_at,
    )
    session.add(ticket)
    await session.commit()
    await session.refresh(ticket, attribute_names=["site"])
    return _out(ticket, datetime.now(UTC))


@router.patch("/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: int, body: TicketPatch, session: AsyncSession = Depends(get_session)
) -> TicketOut:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "unknown ticket")
    ticket.status = body.status
    ticket.closed_at = datetime.now(UTC) if body.status == "closed" else None
    await session.commit()
    await session.refresh(ticket, attribute_names=["site"])
    return _out(ticket, datetime.now(UTC))
