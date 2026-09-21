"""One client, one site, printed keys. Run once: python seed.py"""

import asyncio

from hub.db import SessionLocal
from hub.migrate import upgrade
from hub.models import Client, Site


async def main() -> None:
    async with SessionLocal() as session:
        client = Client(name="Demo Client", contact_email="demo@example.com")
        session.add(client)
        await session.flush()

        site = Site(client_id=client.id, name="demo-api", env="production")
        session.add(site)
        await session.commit()

        print(f"ingest_key    = {site.ingest_key}")
        print(f"ingest_secret = {site.ingest_secret}")


# Blocking, and Alembic runs its own event loop, so it goes before ours.
upgrade()
asyncio.run(main())
