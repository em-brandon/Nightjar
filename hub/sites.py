from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .audit import _where, record
from .db import get_session
from .ingest import record_event
from .models import Client, Event, Issue, Site, _key
from .schemas import EventIn, SiteCreated, SiteIn, SiteListed, SiteOut, SitePatch, TestEventOut

# Creating a site used to be seed.py plus a hand-run UPDATE for the url, and forgetting the
# url left a site silently unmonitored. This is the console-side replacement for both.
router = APIRouter(prefix="/api/sites", tags=["sites"])

STAGING_SUFFIX = "-staging"
TEST_EVENT_TYPE = "NightjarTestEvent"


def _out(site: Site) -> SiteOut:
    return SiteOut(
        id=site.id,
        name=site.name,
        client=site.client.name,
        env=site.env,
        url=site.url,
        public_key=site.public_key,
        allowed_origins=site.allowed_origins or [],
    )


@router.get("", response_model=list[SiteListed])
async def list_sites(session: AsyncSession = Depends(get_session)) -> list[SiteListed]:
    # The console's own test events are left out: "last reported" must mean the app itself reported,
    # or pressing the test button would make a site that was never wired up look as if it were.
    last_event = (
        select(Issue.site_id, func.max(Event.occurred_at).label("at"))
        .join(Event, Event.issue_id == Issue.id)
        .where(Issue.title.not_like(f"{TEST_EVENT_TYPE}%"))
        .group_by(Issue.site_id)
        .subquery()
    )
    rows = await session.execute(
        select(Site, last_event.c.at)
        .options(selectinload(Site.client))
        .outerjoin(last_event, last_event.c.site_id == Site.id)
        .where(Site.env.in_(("production", "staging")))
        .order_by(Site.created_at.desc(), Site.id.desc())
    )
    return [
        SiteListed(**_out(site).model_dump(), ingest_key=site.ingest_key, created_at=site.created_at, last_event=at)
        for site, at in rows.all()
    ]


@router.post("", response_model=SiteCreated, status_code=201)
async def create_site(body: SiteIn, session: AsyncSession = Depends(get_session)) -> SiteCreated:
    # The Staging tab pairs a staging site with its production site by name, so the
    # name has to say which one it is. Rejecting here beats a promotion that never lines up.
    if body.env == "staging" and not body.name.endswith(STAGING_SUFFIX):
        raise HTTPException(422, f"a staging site must be named <production>{STAGING_SUFFIX}")
    if body.env == "production" and body.name.endswith(STAGING_SUFFIX):
        raise HTTPException(422, f"only a staging site may end in {STAGING_SUFFIX}")

    # Names are not unique in the schema, but tickets and promotion look sites up by name.
    # Checked here rather than with a constraint: one operator, so the race is not real.
    if await session.scalar(select(Site.id).where(Site.name == body.name)):
        raise HTTPException(409, f"a site named {body.name} already exists")

    client = await session.scalar(select(Client).where(Client.name == body.client))
    if client is None:
        client = Client(name=body.client, contact_email=body.contact_email)
        session.add(client)
        await session.flush()
    elif body.contact_email and not client.contact_email:
        client.contact_email = body.contact_email

    site = Site(client_id=client.id, name=body.name, env=body.env, url=body.url)
    session.add(site)
    await session.flush()
    record(session, site, "site_created", f"{site.env}, url {_where(site.url)}")
    await session.commit()
    return SiteCreated(
        id=site.id,
        name=site.name,
        client=client.name,
        env=site.env,
        url=site.url,
        public_key=site.public_key,
        allowed_origins=site.allowed_origins or [],
        ingest_key=site.ingest_key,
        ingest_secret=site.ingest_secret,
    )


@router.patch("/{site_id:int}", response_model=SiteOut)
async def update_site(
    site_id: int, body: SitePatch, session: AsyncSession = Depends(get_session)
) -> SiteOut:
    site = await session.scalar(
        select(Site).options(selectinload(Site.client)).where(Site.id == site_id)
    )
    # media and home hosts are not client sites, and have no url to set
    if site is None or site.env not in ("production", "staging"):
        raise HTTPException(404, "unknown site")
    if "url" in body.model_fields_set:
        if site.url != body.url:
            record(session, site, "url_changed", f"{_where(site.url)} -> {_where(body.url)}")
        site.url = body.url
    if body.allowed_origins is not None:
        if body.allowed_origins != (site.allowed_origins or []):
            record(session, site, "origins_changed", ", ".join(body.allowed_origins) or "none")
        site.allowed_origins = body.allowed_origins
        if body.allowed_origins and site.public_key is None:
            site.public_key = _key()
            record(session, site, "browser_key_issued")
        elif not body.allowed_origins:
            if site.public_key is not None:
                record(session, site, "browser_key_revoked")
            # Turning it off revokes the key. Turning it on again makes a new one, which is the
            # rotation path: a key that leaked stops working the moment its pages are removed.
            site.public_key = None
    await session.commit()
    return _out(site)


async def _client_site(session: AsyncSession, site_id: int) -> Site:
    site = await session.scalar(
        select(Site).options(selectinload(Site.client)).where(Site.id == site_id)
    )
    if site is None or site.env not in ("production", "staging"):
        raise HTTPException(404, "unknown site")
    return site


@router.post("/{site_id:int}/rotate-secret", response_model=SiteCreated)
async def rotate_secret(site_id: int, session: AsyncSession = Depends(get_session)) -> SiteCreated:
    # Replaces the hand-run UPDATE in docs/KEYS.md. Signatures made with the old secret stop
    # verifying at once, so the response is the only place the new one is ever shown.
    site = await _client_site(session, site_id)
    site.ingest_secret = _key()
    record(session, site, "secret_rotated")
    await session.commit()
    return SiteCreated(**_out(site).model_dump(), ingest_key=site.ingest_key, ingest_secret=site.ingest_secret)


@router.post("/{site_id:int}/test-event", response_model=TestEventOut)
async def test_event(site_id: int, session: AsyncSession = Depends(get_session)) -> TestEventOut:
    """Send one `warning` through the real ingest path (fingerprint, dedupe, storage), so a new
    site can be proven end to end. A warning is P3, digest only, so it can never wake the phone."""
    site = await _client_site(session, site_id)
    result = await record_event(
        session,
        site,
        EventIn(
            exception_type=TEST_EVENT_TYPE,
            message="Sent from the console to check this site is wired up. Safe to ignore.",
            level="warning",
            environment=site.env,
        ),
    )
    # Muted straight away, not resolved: a muted issue keeps counting but never reports a state
    # change, so it never sits in a client's open list and a second test cannot reopen it as a
    # "regression" that would reach the digest.
    issue = await session.get(Issue, result.issue_id)
    if issue is not None and issue.status == "open":
        issue.status = "muted"
        await session.commit()
    return TestEventOut(
        issue_id=result.issue_id,
        event_count=result.event_count,
        severity=result.severity,
        state_change=result.state_change,
    )
