from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .ingest import _load_site, _verify
from .derive import site_secret
from .models import Deploy, Site
from .schemas import DeployIn, DeployOut

# CI calls the ingest side with the site's key, signed the same way as crash
# events. The console reads the api side.
ingest_router = APIRouter(prefix="/ingest", tags=["deploys"])
router = APIRouter(prefix="/api", tags=["deploys"])

DEPLOY_LIMIT = 50


@ingest_router.post("/{ingest_key}/deploy", response_model=DeployOut, status_code=201)
async def record_deploy(
    ingest_key: str,
    request: Request,
    x_hub_signature: str | None = Header(default=None),
    x_hub_timestamp: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> DeployOut:
    raw = await request.body()
    site = await _load_site(session, ingest_key)
    _verify(site_secret(site), raw, x_hub_signature, x_hub_timestamp)
    try:
        payload = DeployIn.model_validate_json(raw)
    except ValidationError as exc:
        # 422 like any other bad input, not a 500 that a CI job would retry
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc

    # progress means something only while running: a finished deploy is complete or it is not
    progress = payload.progress if payload.status == "running" else None
    deploy = Deploy(site_id=site.id, ref=payload.ref, status=payload.status, progress=progress)
    session.add(deploy)
    await session.commit()
    return DeployOut(
        id=deploy.id,
        site=site.name,
        env=site.env,
        ref=deploy.ref,
        status=deploy.status,
        created_at=deploy.created_at,
        progress=deploy.progress,
    )


@router.get("/deploys", response_model=list[DeployOut])
async def list_deploys(
    env: Literal["production", "staging", "all"] = "all",
    session: AsyncSession = Depends(get_session),
) -> list[DeployOut]:
    query = (
        select(Deploy, Site.name, Site.env)
        .join(Site, Deploy.site_id == Site.id)
        .order_by(Deploy.created_at.desc(), Deploy.id.desc())
        .limit(DEPLOY_LIMIT)
    )
    if env != "all":
        query = query.where(Site.env == env)
    return [
        DeployOut(
            id=d.id, site=name, env=site_env, ref=d.ref, status=d.status, created_at=d.created_at,
            progress=d.progress,
        )
        for d, name, site_env in await session.execute(query)
    ]
