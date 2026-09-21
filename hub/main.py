import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI

from . import auth
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
    # Refuse to run in production with the console open, before anything else.
    auth.check_config()

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


# With auth on, the API docs would describe every route to anyone who asks.
docs = {} if not auth.enabled() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="incident hub", lifespan=lifespan, **docs)

# /ingest authenticates by per-site key and HMAC, and /healthz is open. Everything
# the console reads or writes is under /api and needs the console token.
console = [Depends(auth.require_console)]
app.include_router(auth.router)
app.include_router(ingest_router)
app.include_router(api_router, dependencies=console)
app.include_router(incidents_router, dependencies=console)
app.include_router(tickets_router, dependencies=console)
app.include_router(deploy_ingest_router)
app.include_router(deploys_router, dependencies=console)
app.include_router(stack_ingest_router)
app.include_router(stacks_router, dependencies=console)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
