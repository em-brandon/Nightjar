"""Where signed reports come from and which ones were refused, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_sources -v
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate, sources  # noqa: E402
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


BODY = json.dumps({"exception_type": "ValueError", "message": "boom", "frames": [], "level": "error"}).encode()


def signed(secret: str, body: bytes = BODY) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Hub-Timestamp": ts, "X-Hub-Signature": sig}


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Sources(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_sources_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for p in (
            patch.object(settings, "console_token", ""),
            patch.object(settings, "verify_signatures", True),
            patch.object(settings, "require_signed_timestamp", False),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        # the refused-request counter uses its own session, so point that at the throwaway database too
        p = patch.object(sources, "SessionLocal", self.maker)
        p.start()
        self.addCleanup(p.stop)
        sources._last_written.clear()

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE clients, sites, audit_log, ingest_sources, ingest_failures RESTART IDENTITY CASCADE")
            )
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        made = (await self.http.post("/api/sites", json={"name": "kubasu-motors", "client": "Kubasu", "env": "production"})).json()
        self.sid, self.key, self.secret = made["id"], made["ingest_key"], made["ingest_secret"]

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def report(self, ip: str | None, secret: str | None = None, extra: dict | None = None):
        headers = signed(secret or self.secret)
        if ip:
            headers["X-Forwarded-For"] = ip
        headers.update(extra or {})
        return await self.http.post(f"/ingest/{self.key}/event", content=BODY, headers=headers)

    async def trail(self) -> list[dict]:
        return list(reversed((await self.http.get(f"/api/sites/{self.sid}/audit")).json()))  # oldest first

    async def listed(self) -> dict:
        return next(s for s in (await self.http.get("/api/sites")).json() if s["id"] == self.sid)

    async def count(self, table: str) -> int:
        async with self.engine.connect() as conn:
            return (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar()

    # ---- where reports come from

    async def test_the_first_address_is_a_baseline_and_says_nothing(self):
        self.assertEqual((await self.report("203.0.113.5")).status_code, 200)
        self.assertEqual(await self.count("ingest_sources"), 1)
        self.assertEqual([r["action"] for r in await self.trail()], ["site_created"])
        self.assertIsNone((await self.listed())["new_source_at"])

    async def test_the_same_address_again_is_still_one_source(self):
        await self.report("203.0.113.5")
        sources._last_written.clear()  # past the one-write-a-minute throttle
        await self.report("203.0.113.5")
        self.assertEqual(await self.count("ingest_sources"), 1)
        async with self.engine.connect() as conn:
            reports = (await conn.execute(text("SELECT reports FROM ingest_sources"))).scalar()
        self.assertEqual(reports, 2)
        self.assertEqual([r["action"] for r in await self.trail()], ["site_created"])

    async def test_a_burst_from_one_address_is_throttled_to_one_write(self):
        for _ in range(5):
            await self.report("203.0.113.5")
        async with self.engine.connect() as conn:
            reports = (await conn.execute(text("SELECT reports FROM ingest_sources"))).scalar()
        self.assertEqual(reports, 1)

    async def test_a_new_address_after_the_baseline_is_recorded_and_shown(self):
        await self.report("203.0.113.5")
        await self.report("198.51.100.7")
        trail = await self.trail()
        self.assertEqual([r["action"] for r in trail], ["site_created", "new_source"])
        self.assertEqual(trail[-1]["detail"], "198.51.100.7")
        self.assertIsNotNone((await self.listed())["new_source_at"])
        self.assertEqual(await self.count("ingest_sources"), 2)

    async def test_the_cloudflare_header_wins_over_forwarded_for(self):
        await self.report("203.0.113.5", extra={"CF-Connecting-IP": "192.0.2.9"})
        async with self.engine.connect() as conn:
            ip = (await conn.execute(text("SELECT ip FROM ingest_sources"))).scalar()
        self.assertEqual(ip, "192.0.2.9")

    async def test_a_site_is_judged_against_its_own_addresses_only(self):
        other = (await self.http.post("/api/sites", json={"name": "other-site", "client": "Kubasu", "env": "production"})).json()
        await self.report("203.0.113.5")
        headers = {**signed(other["ingest_secret"]), "X-Forwarded-For": "198.51.100.7"}
        await self.http.post(f"/ingest/{other['ingest_key']}/event", content=BODY, headers=headers)
        # the other site's first address is its baseline, not a new source for this one
        self.assertEqual([r["action"] for r in await self.trail()], ["site_created"])

    # ---- refused requests

    async def test_a_bad_signature_is_refused_counted_and_shown(self):
        r = await self.report("203.0.113.5", secret="not-the-secret")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(await self.count("ingest_sources"), 0, "a refused request is not a source")
        listed = await self.listed()
        self.assertEqual(listed["bad_signatures_24h"], 1)
        trail = await self.trail()
        self.assertEqual(trail[-1]["action"], "refused_request")
        self.assertEqual(trail[-1]["detail"], "203.0.113.5: bad signature")

    async def test_repeats_are_counted_but_not_listed_again(self):
        for _ in range(4):
            await self.report("203.0.113.5", secret="nope")
        self.assertEqual((await self.listed())["bad_signatures_24h"], 4)
        self.assertEqual([r["action"] for r in await self.trail()].count("refused_request"), 1)

    async def test_a_flood_from_many_addresses_cannot_flood_the_history(self):
        for i in range(12):
            await self.report(f"203.0.113.{i + 1}", secret="nope")
        self.assertEqual((await self.listed())["bad_signatures_24h"], 12)
        self.assertEqual([r["action"] for r in await self.trail()].count("refused_request"), sources.AUDIT_PER_HOUR)

    async def test_a_missing_signature_and_a_stale_timestamp_are_counted_with_their_reasons(self):
        r = await self.http.post(f"/ingest/{self.key}/event", content=BODY, headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 401)
        old = signed(self.secret)
        old["X-Hub-Timestamp"] = str(int(time.time()) - 3600)
        self.assertEqual((await self.http.post(f"/ingest/{self.key}/event", content=BODY, headers=old)).status_code, 401)
        async with self.engine.connect() as conn:
            reasons = sorted(x[0] for x in (await conn.execute(text("SELECT reason FROM ingest_failures"))).all())
        self.assertEqual(reasons, ["missing signature", "stale timestamp"])

    async def test_an_unknown_key_records_nothing(self):
        r = await self.http.post("/ingest/no-such-key/event", content=BODY, headers=signed("x"))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(await self.count("ingest_failures"), 0)

    async def test_nothing_secret_is_ever_stored(self):
        await self.report("203.0.113.5", secret="a-guessed-secret-value")
        await self.report("203.0.113.5")
        async with self.engine.connect() as conn:
            dump = ""
            for table in ("ingest_failures", "ingest_sources", "audit_log"):
                dump += str((await conn.execute(text(f"SELECT * FROM {table}"))).all())
        for secret in (self.secret, "a-guessed-secret-value", "kaboom", "sha256="):
            self.assertNotIn(secret, dump)

    async def test_recording_failing_never_breaks_ingest(self):
        # Both recorders swallow their own failures: a broken session, and a site that does not exist (a foreign key
        # violation inside their own transaction). Neither may raise into the ingest route.
        ghost = type("Site", (), {"id": 999_999})()
        with self.assertLogs("nightjar.sources", level="ERROR") as logs:
            self.assertIsNone(await sources.note_source(None, ghost, "1.2.3.4"))
            self.assertIsNone(await sources.note_failure(ghost, "1.2.3.4", "bad signature"))
        self.assertEqual(len(logs.records), 2, "both failures are logged, not raised")
        self.assertEqual((await self.report("203.0.113.5")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
