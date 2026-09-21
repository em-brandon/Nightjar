"""Deploy reports with progress, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_deploy_progress -v
"""

import asyncio
import os
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

KEY = "staging-key"
URL = f"/ingest/{KEY}/deploy"


def _admin_url() -> str:
    return make_url(settings.database_url).set(database="postgres").render_as_string(hide_password=False)


async def _admin(sql: str) -> None:
    engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def _reachable() -> bool:
    try:
        asyncio.run(_admin("SELECT 1"))
        return True
    except Exception:
        return False


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class DeployProgress(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_dep_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        p = patch.object(settings, "console_token", "")
        p.start()
        self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, deploys RESTART IDENTITY CASCADE"))
            await conn.execute(text("INSERT INTO clients (name) VALUES ('c')"))
            await conn.execute(
                text("INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) "
                     f"VALUES (1, 'shop-staging', 'staging', '{KEY}', 's')")
            )
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def test_a_running_deploy_carries_its_progress(self):
        r = await self.http.post(URL, json={"ref": "develop@abc", "status": "running", "progress": 40})
        self.assertEqual((r.status_code, r.json()["progress"]), (201, 40))
        latest = (await self.http.get("/api/deploys?env=staging")).json()[0]
        self.assertEqual((latest["status"], latest["progress"]), ("running", 40))

    async def test_progress_is_optional(self):
        r = await self.http.post(URL, json={"ref": "develop@abc", "status": "running"})
        self.assertEqual((r.status_code, r.json()["progress"]), (201, None))

    async def test_a_finished_deploy_has_no_progress(self):
        # a stray progress on a finished deploy is dropped: it is complete, or it failed
        for status in ("success", "failed"):
            r = await self.http.post(URL, json={"ref": "develop@abc", "status": status, "progress": 60})
            self.assertEqual((r.status_code, r.json()["progress"]), (201, None), status)

    async def test_progress_must_be_a_percentage(self):
        for bad in (-1, 101, "half"):
            r = await self.http.post(URL, json={"ref": "x", "status": "running", "progress": bad})
            self.assertEqual(r.status_code, 422, bad)


if __name__ == "__main__":
    unittest.main()
