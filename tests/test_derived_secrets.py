"""Derived site secrets end to end, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_derived_secrets -v
"""

import asyncio
import hashlib
import hmac
import time
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

from hub import derive, migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

MASTER = "k" * 40


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
class DerivedSecrets(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_derive_{uuid.uuid4().hex[:10]}"
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

    def key(self, value=MASTER):
        return patch.object(settings, "secret_master_key", value)

    async def status_with(self, made, secret):
        body = b'{"exception_type": "Boom", "message": "x"}'
        ts = str(int(time.time()))
        sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        with patch.object(settings, "verify_signatures", True):
            r = await self.http.post(
                f"/ingest/{made['ingest_key']}/event",
                content=body,
                headers={"X-Hub-Signature": sig, "X-Hub-Timestamp": ts, "Content-Type": "application/json"},
            )
        return r.status_code

    async def row(self, sid):
        async with self.engine.connect() as conn:
            return (
                await conn.execute(text("SELECT ingest_secret, secret_version FROM sites WHERE id = :i"), {"i": sid})
            ).one()

    async def test_a_new_site_gets_a_derived_secret_that_is_not_in_the_database(self):
        with self.key():
            made = (await self.http.post("/api/sites", json=site())).json()
            stored, version = await self.row(made["id"])
            self.assertEqual((stored, version), ("", 1))
            self.assertEqual(made["ingest_secret"], derive.derive(MASTER, made["id"], 1))
            self.assertEqual(await self.status_with(made, made["ingest_secret"]), 200)
            self.assertEqual(await self.status_with(made, "wrong"), 401)
        async with self.engine.connect() as conn:
            dump = str((await conn.execute(text("SELECT * FROM sites"))).all())
        self.assertNotIn(made["ingest_secret"], dump)

    async def test_without_a_master_key_a_new_site_keeps_a_stored_secret_as_before(self):
        with self.key(""):
            made = (await self.http.post("/api/sites", json=site())).json()
            stored, version = await self.row(made["id"])
            self.assertEqual((stored, version), (made["ingest_secret"], 0))
            self.assertEqual(await self.status_with(made, made["ingest_secret"]), 200)

    async def test_a_legacy_site_keeps_verifying_after_a_master_key_is_set(self):
        with self.key(""):
            made = (await self.http.post("/api/sites", json=site())).json()
        with self.key():
            self.assertEqual(await self.status_with(made, made["ingest_secret"]), 200)

    async def test_rotating_a_legacy_site_moves_it_to_a_derived_secret_and_refuses_the_old_one(self):
        with self.key(""):
            made = (await self.http.post("/api/sites", json=site())).json()
        with self.key():
            new = (await self.http.post(f"/api/sites/{made['id']}/rotate-secret")).json()
            self.assertEqual(new["ingest_secret"], derive.derive(MASTER, made["id"], 1))
            self.assertEqual(await self.row(made["id"]), ("", 1))
            self.assertEqual(await self.status_with(made, made["ingest_secret"]), 401)
            self.assertEqual(await self.status_with(made, new["ingest_secret"]), 200)

    async def test_rotating_a_derived_site_bumps_the_version_and_refuses_the_previous_secret(self):
        with self.key():
            made = (await self.http.post("/api/sites", json=site())).json()
            second = (await self.http.post(f"/api/sites/{made['id']}/rotate-secret")).json()
            self.assertEqual((await self.row(made["id"]))[1], 2)
            self.assertNotEqual(second["ingest_secret"], made["ingest_secret"])
            self.assertEqual(await self.status_with(made, made["ingest_secret"]), 401)
            self.assertEqual(await self.status_with(made, second["ingest_secret"]), 200)

    async def test_rotating_without_a_master_key_stays_stored(self):
        with self.key(""):
            made = (await self.http.post("/api/sites", json=site())).json()
            new = (await self.http.post(f"/api/sites/{made['id']}/rotate-secret")).json()
            self.assertEqual(await self.row(made["id"]), (new["ingest_secret"], 0))

    async def test_a_derived_site_cannot_report_if_the_master_key_goes_missing(self):
        with self.key():
            made = (await self.http.post("/api/sites", json=site())).json()
        with self.key(""):
            self.assertEqual(await self.status_with(made, made["ingest_secret"]), 503)
            self.assertEqual(await self.status_with(made, ""), 503)

    async def test_a_different_master_key_refuses_every_derived_secret(self):
        with self.key():
            made = (await self.http.post("/api/sites", json=site())).json()
        with self.key("z" * 40):
            self.assertEqual(await self.status_with(made, made["ingest_secret"]), 401)

    async def test_two_sites_get_different_secrets(self):
        with self.key():
            a = (await self.http.post("/api/sites", json=site())).json()
            b = (await self.http.post("/api/sites", json=site(name="other", client="Other"))).json()
            self.assertNotEqual(a["ingest_secret"], b["ingest_secret"])
            self.assertEqual(await self.status_with(a, b["ingest_secret"]), 401)

    async def test_the_hub_refuses_to_start_when_derived_sites_exist_and_the_key_is_gone(self):
        with patch.object(derive, "SessionLocal", self.maker):
            with self.key(""):
                await derive.check_database()  # no derived sites yet: fine
                legacy = (await self.http.post("/api/sites", json=site(name="old", client="Old"))).json()
                self.assertTrue(legacy["id"])
                await derive.check_database()  # a legacy site needs no key
            with self.key():
                await self.http.post("/api/sites", json=site())
                await derive.check_database()  # key present: fine
            with self.key(""):
                with self.assertRaises(RuntimeError) as ctx:
                    await derive.check_database()
        self.assertIn("SECRET_MASTER_KEY", str(ctx.exception))

    async def test_the_audit_trail_names_the_version_and_never_the_secret(self):
        with self.key():
            made = (await self.http.post("/api/sites", json=site())).json()
            new = (await self.http.post(f"/api/sites/{made['id']}/rotate-secret")).json()
        trail = (await self.http.get(f"/api/sites/{made['id']}/audit")).text
        self.assertIn("derived, version 2", trail)
        self.assertNotIn(new["ingest_secret"], trail)
        self.assertNotIn(made["ingest_secret"], trail)


if __name__ == "__main__":
    unittest.main()
