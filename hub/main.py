import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .api import router as api_router
from .config import settings
from .db import engine
from .deploys import ingest_router as deploy_ingest_router
from .deploys import router as deploys_router
from .incidents import router as incidents_router
from .ingest import router as ingest_router
from .migrate import upgrade
from .probe import probe_loop
from .stacks import ingest_router as stack_ingest_router
from .stacks import router as stacks_router
from .tickets import router as tickets_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migrate on every start. Alembic runs its own event loop, so it gets a
    # thread. A database this cannot migrate raises here, and the hub refuses to
    # start: better than serving requests against the wrong schema.
    await asyncio.to_thread(upgrade)
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
app.include_router(stack_ingest_router)
app.include_router(stacks_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
