"""The stack-report endpoint, end to end, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest discover -s tests -v

Every call asserts its HTTP status. That is the point of this file: an earlier check
of this flow looked only at the database, so a bug that answered every healthy report
with a 500 (after the row had already been saved) went unnoticed.
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import timedelta  # noqa: F401  (kept for readers extending the tests)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

KEY = "test-key"
URL = f"/ingest/{KEY}/stack"


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
class StackIngest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # one migrated database for the whole class: creating one per test is slow
        cls.name = f"nj_stack_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        # pooled, so queries reuse a connection instead of opening one each (slow through Docker)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        await self.sql(
            "TRUNCATE clients, sites, issues, events, issue_actions, stack_reports RESTART IDENTITY CASCADE"
        )
        await self.sql("INSERT INTO clients (name) VALUES ('c')")
        await self.sql(
            "INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) "
            f"VALUES (1, 'shop-staging', 'staging', '{KEY}', 's')"
        )
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def sql(self, statement):
        async with self.engine.begin() as conn:
            result = await conn.execute(text(statement))
            return result.all() if result.returns_rows else None

    async def report(self, stack="web", state="running", **fields):
        body = {"stack": stack, "state": state, "mem_mb": 100, **fields}
        return await self.http.post(URL, json=body)

    async def issue(self, stack="web"):
        rows = await self.sql(
            f"SELECT status, event_count FROM issues WHERE title = 'StackDegraded: stack {stack} is degraded'"
        )
        return rows[0] if rows else None

    async def backfill_healthy(self, stack="web", minutes=11):
        """A healthy report every minute for the last `minutes`, oldest first."""
        for s in range(minutes * 60, 59, -60):
            await self.sql(
                "INSERT INTO stack_reports (site_id, reported_at, stack, state, mem_mb, services) "
                f"VALUES (1, now() - interval '{s} seconds', '{stack}', 'running', 100, '[]'::jsonb)"
            )

    # ------------------------------------------------------------------ status codes

    async def test_a_healthy_report_is_201_and_returns_its_id(self):
        # the exact case that used to 500: nothing to resolve, so the session was
        # rolled back and the response then read an expired object
        r = await self.report()
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIsInstance(r.json()["id"], int)

    async def test_every_report_in_a_full_lifecycle_is_201(self):
        for state in ("running", "degraded", "degraded", "running", "degraded", "stopped", "running"):
            r = await self.report(state=state)
            self.assertEqual(r.status_code, 201, f"{state}: {r.text}")

    async def test_ids_are_distinct_and_increasing(self):
        ids = [(await self.report()).json()["id"] for _ in range(3)]
        self.assertEqual(ids, sorted(set(ids)))

    async def test_bad_input_and_unknown_key(self):
        self.assertEqual((await self.report(state="exploded")).status_code, 422)
        self.assertEqual((await self.report(restarts=-1)).status_code, 422)
        self.assertEqual((await self.http.post("/ingest/no-such-key/stack", json={"stack": "x", "state": "running"})).status_code, 404)

    # ------------------------------------------------------------------ behaviour

    async def test_degraded_opens_one_issue_and_repeats_add_no_events(self):
        self.assertEqual((await self.report(state="degraded")).status_code, 201)
        self.assertEqual(await self.issue(), ("open", 1))
        for _ in range(4):
            self.assertEqual((await self.report(state="degraded")).status_code, 201)
        self.assertEqual(await self.issue(), ("open", 1), "edge-triggered: staying degraded is not a new event")

    async def test_a_relapse_after_recovery_is_a_new_event_on_the_same_issue(self):
        await self.report(state="degraded")
        await self.report(state="running")
        self.assertEqual((await self.report(state="degraded")).status_code, 201)
        self.assertEqual(await self.issue(), ("open", 2))

    async def test_ten_healthy_minutes_resolve_it_and_the_response_is_still_201(self):
        self.assertEqual((await self.report(state="degraded")).status_code, 201)
        # move the degraded report out of the way, then give it 11 clean minutes
        await self.sql("UPDATE stack_reports SET reported_at = now() - interval '25 minutes' WHERE state = 'degraded'")
        await self.backfill_healthy()

        r = await self.report()  # the report that triggers the auto-resolve commit path
        self.assertEqual(r.status_code, 201, r.text)

        self.assertEqual(await self.issue(), ("resolved", 1))
        action = await self.sql("SELECT action, detail FROM issue_actions")
        self.assertEqual(action, [("resolve", "auto: web has been healthy for 10 minutes")])

    async def test_not_enough_history_does_not_resolve(self):
        await self.report(state="degraded")
        self.assertEqual((await self.report()).status_code, 201)
        self.assertEqual(await self.issue(), ("open", 1))

    async def test_a_muted_issue_is_left_alone(self):
        await self.report(state="degraded")
        await self.sql("UPDATE issues SET status = 'muted'")
        await self.sql("UPDATE stack_reports SET reported_at = now() - interval '25 minutes'")
        await self.backfill_healthy()
        self.assertEqual((await self.report()).status_code, 201)
        self.assertEqual(await self.issue(), ("muted", 1))

    async def test_a_gap_in_the_reports_does_not_resolve(self):
        await self.report(state="degraded")
        await self.sql("UPDATE stack_reports SET reported_at = now() - interval '25 minutes'")
        # a five-minute silence in the middle of the window
        for s in (660, 600, 540, 480, 120, 60):
            await self.sql(
                "INSERT INTO stack_reports (site_id, reported_at, stack, state, mem_mb, services) "
                f"VALUES (1, now() - interval '{s} seconds', 'web', 'running', 100, '[]'::jsonb)"
            )
        self.assertEqual((await self.report()).status_code, 201)
        self.assertEqual(await self.issue(), ("open", 1))


if __name__ == "__main__":
    unittest.main()
