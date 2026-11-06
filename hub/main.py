import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI

from . import auth, derive
from .alerts import router as alerts_router
from .api import router as api_router
from .browser import router as browser_router
from .config import settings
from .db import engine
from .deploys import ingest_router as deploy_ingest_router
from .deploys import router as deploys_router
from .heartbeats import ingest_router as heartbeat_ingest_router
from .heartbeats import router as heartbeats_router
from .incidents import router as incidents_router
from .home import ingest_router as home_ingest_router
from .home import router as home_router
from .ingest import router as ingest_router
from .media import ingest_router as media_ingest_router
from .media import router as media_router
from .migrate import upgrade
from .outbox import sender_loop
from .presence import public_router as presence_public_router
from .presence import router as presence_router
from .probe import probe_loop
from .pushover import router as pushover_router
from .rates import router as rates_router
from .reports import router as reports_router
from .sites import router as sites_router
from .audit import router as audit_router
from .stacks import ingest_router as stack_ingest_router
from .stacks import router as stacks_router
from .tickets import router as tickets_router
from .users import router as users_router
from .users import setup_router
from .weather import router as weather_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to run in production with the console open, before anything else.
    auth.check_config()

    # Migrate on every start. Alembic runs its own event loop, so it gets a
    # thread. A database this cannot migrate raises here, and the hub refuses to
    # start: better than serving requests against the wrong schema.
    await asyncio.to_thread(upgrade)
    await derive.check_database()
    await auth.check_operators()
    prober = asyncio.create_task(probe_loop()) if settings.probe_enabled else None
    # The alert sender. Alerts are queued whether or not this runs; it only delivers them.
    sender = asyncio.create_task(sender_loop()) if settings.outbox_enabled else None
    yield
    for task in (prober, sender):
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    await engine.dispose()


# With auth on, the API docs would describe every route to anyone who asks.
docs = {} if not auth.enabled() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="incident hub", lifespan=lifespan, **docs)

# /ingest authenticates by per-site key and HMAC, and /healthz is open. Everything
# the console reads or writes is under /api and needs the console token.
console = [Depends(auth.require_console)]
app.include_router(auth.router)
# Setting up a new account is authenticated by its one-time setup token, not by a session
app.include_router(setup_router)
app.include_router(users_router, dependencies=console)
app.include_router(browser_router)
app.include_router(ingest_router)
app.include_router(api_router, dependencies=console)
app.include_router(incidents_router, dependencies=console)
app.include_router(sites_router, dependencies=console)
app.include_router(audit_router, dependencies=console)
app.include_router(alerts_router, dependencies=console)
app.include_router(tickets_router, dependencies=console)
app.include_router(deploy_ingest_router)
app.include_router(deploys_router, dependencies=console)
app.include_router(stack_ingest_router)
app.include_router(stacks_router, dependencies=console)
app.include_router(heartbeat_ingest_router)
app.include_router(heartbeats_router, dependencies=console)
app.include_router(media_ingest_router)
app.include_router(media_router, dependencies=console)
app.include_router(weather_router, dependencies=console)
app.include_router(home_ingest_router)
app.include_router(home_router, dependencies=console)
app.include_router(presence_public_router)
# Pushover cannot send the console token: this route holds its own secret in the path (hub/pushover.py)
app.include_router(pushover_router)
app.include_router(presence_router, dependencies=console)
app.include_router(rates_router, dependencies=console)
app.include_router(reports_router, dependencies=console)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
