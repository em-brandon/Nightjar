"""The audit trail for sites and secrets, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_audit -v
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


def site(**over):
    return {"name": "kubasu-motors", "client": "Kubasu Motors", "env": "production", **over}


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Audit(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_audit_{uuid.uuid4().hex[:10]}"
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
            await conn.execute(text("TRUNCATE clients, sites, audit_log RESTART IDENTITY CASCADE"))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def trail(self, sid):
        r = await self.http.get(f"/api/sites/{sid}/audit")
        self.assertEqual(r.status_code, 200, r.text)
        return list(reversed(r.json()))  # oldest first

    async def test_creating_a_site_writes_a_row(self):
        made = (await self.http.post("/api/sites", json=site(url="https://kubasu.example/"))).json()
        rows = await self.trail(made["id"])
        self.assertEqual([r["action"] for r in rows], ["site_created"])
        self.assertEqual(rows[0]["actor"], "console")

    async def test_each_change_writes_one_row(self):
        sid = (await self.http.post("/api/sites", json=site())).json()["id"]
        await self.http.patch(f"/api/sites/{sid}", json={"url": "https://kubasu.example/"})
        await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": ["https://kubasu.example"]})
        await self.http.post(f"/api/sites/{sid}/rotate-secret")
        await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": []})
        actions = [r["action"] for r in await self.trail(sid)]
        self.assertEqual(
            actions,
            ["site_created", "url_changed", "origins_changed", "browser_key_issued",
             "secret_rotated", "origins_changed", "browser_key_revoked"],
        )

    async def test_a_patch_that_changes_nothing_writes_nothing(self):
        sid = (await self.http.post("/api/sites", json=site(url="https://kubasu.example/"))).json()["id"]
        await self.http.patch(f"/api/sites/{sid}", json={"url": "https://kubasu.example/"})
        await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": []})
        self.assertEqual([r["action"] for r in await self.trail(sid)], ["site_created"])

    async def test_no_row_ever_holds_a_secret(self):
        made = (await self.http.post(
            "/api/sites", json=site(url="https://user:hunter2@kubasu.example/x?token=abc123"))).json()
        sid = made["id"]
        secrets_seen = [made["ingest_secret"]]
        await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": ["https://kubasu.example"]})
        public_key = (await self.http.get("/api/sites")).json()[0]["public_key"]
        rotated = (await self.http.post(f"/api/sites/{sid}/rotate-secret")).json()
        secrets_seen += [rotated["ingest_secret"], rotated["ingest_key"], public_key, "hunter2", "abc123"]
        async with self.engine.connect() as conn:
            dump = str((await conn.execute(text("SELECT * FROM audit_log"))).all())
        for secret in secrets_seen:
            self.assertNotIn(secret, dump)
        self.assertNotIn(made["ingest_secret"], (await self.http.get(f"/api/sites/{sid}/audit")).text)

    async def test_a_failed_change_leaves_no_row(self):
        self.assertEqual((await self.http.post("/api/sites/999/rotate-secret")).status_code, 404)
        self.assertEqual((await self.http.get("/api/sites/999/audit")).status_code, 404)
        async with self.engine.connect() as conn:
            self.assertEqual((await conn.execute(text("SELECT count(*) FROM audit_log"))).scalar(), 0)

    async def test_the_hub_offers_no_way_to_edit_or_delete_the_trail(self):
        sid = (await self.http.post("/api/sites", json=site())).json()["id"]
        for method in ("put", "patch", "delete", "post"):
            r = await getattr(self.http, method)(f"/api/sites/{sid}/audit")
            self.assertEqual(r.status_code, 405, method)

    async def test_the_trail_is_per_site(self):
        a = (await self.http.post("/api/sites", json=site())).json()["id"]
        b = (await self.http.post("/api/sites", json=site(name="other", client="Other"))).json()["id"]
        await self.http.post(f"/api/sites/{b}/rotate-secret")
        self.assertEqual(len(await self.trail(a)), 1)
        self.assertEqual(len(await self.trail(b)), 2)


if __name__ == "__main__":
    unittest.main()
