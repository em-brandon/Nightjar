import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .api import router as api_router
from .config import settings
from .db import Base, engine
from .deploys import ingest_router as deploy_ingest_router
from .deploys import router as deploys_router
from .incidents import router as incidents_router
from .ingest import router as ingest_router
from .probe import probe_loop
from .tickets import router as tickets_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fine for v1. Swap in Alembic before you have data you care about.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    prober = asyncio.create_task(probe_loop()) if settings.probe_enabled else None
    yield
    if prober:
        prober.cancel()
        with suppress(asyncio.CancelledError):
            await prober
    await engine.dispose()


app = FastAPI(title="incident hub", lifespan=lifespan)
app.include_router(ingest_router)
app.include_router(api_router)
app.include_router(incidents_router)
app.include_router(tickets_router)
app.include_router(deploy_ingest_router)
app.include_router(deploys_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
