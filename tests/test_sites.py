"""Creating and editing sites from the console API, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_sites -v
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
class Sites(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_site_{uuid.uuid4().hex[:10]}"
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
            await conn.execute(text("TRUNCATE clients, sites RESTART IDENTITY CASCADE"))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def test_a_new_site_comes_back_with_keys_that_ingest_accepts(self):
        r = await self.http.post("/api/sites", json=site(url="https://kubasu.example/"))
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual((body["name"], body["client"], body["env"]), ("kubasu-motors", "Kubasu Motors", "production"))
        self.assertTrue(body["ingest_key"] and body["ingest_secret"])
        with patch.object(settings, "verify_signatures", False):
            ev = await self.http.post(
                f"/ingest/{body['ingest_key']}/event", json={"exception_type": "Boom", "message": "x"}
            )
        self.assertEqual(ev.status_code, 200, ev.text)

    async def test_the_secret_is_never_listed_afterwards(self):
        await self.http.post("/api/sites", json=site())
        health = (await self.http.get("/api/sites/health")).text
        self.assertNotIn("ingest_secret", health)

    async def test_a_second_site_for_the_same_client_reuses_the_client(self):
        await self.http.post("/api/sites", json=site())
        r = await self.http.post("/api/sites", json=site(name="kubasu-motors-staging", env="staging"))
        self.assertEqual(r.status_code, 201, r.text)
        async with self.engine.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM clients"))).scalar()
        self.assertEqual(n, 1)

    async def test_a_duplicate_name_is_refused(self):
        await self.http.post("/api/sites", json=site())
        r = await self.http.post("/api/sites", json=site(client="Someone Else"))
        self.assertEqual(r.status_code, 409)

    async def test_staging_and_production_names_must_say_which_they_are(self):
        # promotion pairs <production>-staging with <production> by name
        self.assertEqual((await self.http.post("/api/sites", json=site(env="staging"))).status_code, 422)
        bad = site(name="kubasu-motors-staging")
        self.assertEqual((await self.http.post("/api/sites", json=bad)).status_code, 422)

    async def test_names_are_lowercase_slugs(self):
        for bad in ("Kubasu Motors", "UPPER", "a", "-lead", "has/slash"):
            r = await self.http.post("/api/sites", json=site(name=bad))
            self.assertEqual(r.status_code, 422, bad)

    async def test_only_web_urls_are_accepted(self):
        for bad in ("file:///etc/passwd", "kubasu.co.ke", "ftp://x.example/a", "http://has space.example"):
            r = await self.http.post("/api/sites", json=site(url=bad))
            self.assertEqual(r.status_code, 422, bad)

    async def test_the_media_and_home_hosts_cannot_be_made_here(self):
        for env in ("media", "home"):
            r = await self.http.post("/api/sites", json=site(env=env))
            self.assertEqual(r.status_code, 422, env)

    async def test_setting_the_url_starts_monitoring_and_clearing_it_stops(self):
        created = (await self.http.post("/api/sites", json=site())).json()
        health = {s["name"]: s for s in (await self.http.get("/api/sites/health")).json()}
        self.assertFalse(health["kubasu-motors"]["monitored"])

        r = await self.http.patch(f"/api/sites/{created['id']}", json={"url": "https://kubasu.example/health"})
        self.assertEqual((r.status_code, r.json()["url"]), (200, "https://kubasu.example/health"))
        health = {s["name"]: s for s in (await self.http.get("/api/sites/health")).json()}
        self.assertTrue(health["kubasu-motors"]["monitored"])

        await self.http.patch(f"/api/sites/{created['id']}", json={"url": None})
        health = {s["name"]: s for s in (await self.http.get("/api/sites/health")).json()}
        self.assertFalse(health["kubasu-motors"]["monitored"])

    async def test_an_unknown_site_is_404(self):
        r = await self.http.patch("/api/sites/999", json={"url": "https://x.example/"})
        self.assertEqual(r.status_code, 404)

    async def test_the_home_host_site_cannot_be_edited_here(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO clients (name) VALUES ('me')"))
            await conn.execute(
                text("INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) "
                     "VALUES (1, 'thinkcentre', 'home', 'hk', 'hs')")
            )
        r = await self.http.patch("/api/sites/1", json={"url": "https://x.example/"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
