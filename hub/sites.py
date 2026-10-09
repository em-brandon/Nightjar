from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .db import get_session
from .models import Client, Site
from .schemas import SiteCreated, SiteIn, SiteOut, SitePatch

# Creating a site used to be seed.py plus a hand-run UPDATE for the url, and forgetting the
# url left a site silently unmonitored. This is the console-side replacement for both.
router = APIRouter(prefix="/api/sites", tags=["sites"])

STAGING_SUFFIX = "-staging"


def _out(site: Site) -> SiteOut:
    return SiteOut(id=site.id, name=site.name, client=site.client.name, env=site.env, url=site.url)


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
    await session.commit()
    return SiteCreated(
        id=site.id,
        name=site.name,
        client=client.name,
        env=site.env,
        url=site.url,
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
    site.url = body.url
    await session.commit()
    return _out(site)
