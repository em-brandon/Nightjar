"""Alerts as the console sees them: the notifications list and the lines in an incident's log.
Against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_alerts_api -v
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
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
from hub.models import Client, Issue, IssueAction, Notification, Site  # noqa: E402

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


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
class AlertsApi(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_alerts_{uuid.uuid4().hex[:10]}"
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
            await conn.execute(text("TRUNCATE clients, sites, issues, notifications, issue_actions RESTART IDENTITY CASCADE"))
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            site = Site(client_id=client.id, name="acme-shop")
            session.add(site)
            await session.flush()
            issue = Issue(site_id=site.id, fingerprint="f" * 32, title="Boom", severity="P1", first_seen=T0, last_seen=T0)
            session.add(issue)
            await session.commit()
            self.issue_id = issue.id
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self._n = 0

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def add(self, **over) -> int:
        self._n += 1
        values = dict(
            issue_id=self.issue_id, change="new", changed_at=T0 + timedelta(seconds=self._n), severity="P1", priority=2,
            site="acme-shop", title="Boom", status="pending", attempts=0, next_attempt_at=T0,
            created_at=T0 + timedelta(minutes=self._n),
        )
        values.update(over)
        async with self.maker() as session:
            row = Notification(**values)
            session.add(row)
            await session.commit()
            return row.id

    async def listed(self, **params):
        r = await self.http.get("/api/notifications", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---------------------------------------------------------------- the list

    async def test_it_lists_newest_first_with_what_happened(self):
        a = await self.add(status="sent", attempts=1, sent_at=T0)
        b = await self.add(status="failed", attempts=6, last_error="Pushover timed out")
        got = await self.listed()
        self.assertEqual([n["id"] for n in got], [b, a])
        self.assertEqual((got[0]["status"], got[0]["attempts"], got[0]["last_error"]), ("failed", 6, "Pushover timed out"))
        self.assertEqual((got[1]["status"], got[1]["priority"], got[1]["site"]), ("sent", 2, "acme-shop"))

    async def test_failed_is_only_what_gave_up(self):
        await self.add(status="sent", attempts=1)
        failed = await self.add(status="failed", attempts=6)
        await self.add(status="pending", attempts=2)
        self.assertEqual([n["id"] for n in await self.listed(status="failed")], [failed])

    async def test_undelivered_adds_what_has_failed_once_and_is_still_being_retried(self):
        await self.add(status="sent", attempts=1)
        await self.add(status="pending", attempts=0)  # fresh: not a problem yet
        retrying = await self.add(status="pending", attempts=2, last_error="Pushover timed out")
        sending = await self.add(status="sending", attempts=3)
        failed = await self.add(status="failed", attempts=6)
        ids = {n["id"] for n in await self.listed(status="undelivered")}
        self.assertEqual(ids, {retrying, sending, failed})

    async def test_the_limit_is_honoured_and_bounded(self):
        for _ in range(5):
            await self.add()
        self.assertEqual(len(await self.listed(limit=3)), 3)
        r = await self.http.get("/api/notifications", params={"limit": 0})
        self.assertEqual(r.status_code, 422)

    async def test_an_unknown_status_filter_is_refused(self):
        r = await self.http.get("/api/notifications", params={"status": "bogus"})
        self.assertEqual(r.status_code, 422)

    # ---------------------------------------------------------------- the incident log

    async def log(self) -> list[str]:
        r = await self.http.get(f"/api/issues/{self.issue_id}/log")
        self.assertEqual(r.status_code, 200, r.text)
        return [e["text"] for e in r.json()]

    async def test_a_sent_alert_is_in_the_log(self):
        await self.add(status="sent", attempts=1, sent_at=T0 + timedelta(seconds=5))
        self.assertIn("Alert sent to the phone (new issue, emergency priority)", await self.log())

    async def test_a_failed_alert_says_so_loudly_with_the_reason(self):
        await self.add(status="failed", attempts=6, last_error="Pushover is not configured (set PUSHOVER_TOKEN and PUSHOVER_USER)")
        line = next(t for t in await self.log() if "NOT SENT" in t)
        self.assertIn("after 6 attempts", line)
        self.assertIn("not configured", line)

    async def test_an_alert_being_retried_and_one_just_queued_read_differently(self):
        await self.add(status="pending", attempts=2, last_error="Pushover timed out", change="regression", priority=0)
        await self.add(status="pending", attempts=0, change="spike", priority=-1)
        log = await self.log()
        self.assertTrue(any("retrying (attempt 2, back after being resolved, normal priority): Pushover timed out" in t for t in log), log)
        self.assertTrue(any("queued for the phone (spiking, quiet, no sound)" in t for t in log), log)

    async def test_an_acknowledgement_from_the_phone_is_labelled(self):
        async with self.maker() as session:
            session.add(IssueAction(issue_id=self.issue_id, action="ack", detail="phone"))
            session.add(IssueAction(issue_id=self.issue_id, action="ack", detail=""))
            await session.commit()
        log = await self.log()
        self.assertIn("Acknowledged from the phone", log)
        self.assertIn("Acknowledged", log, "a console acknowledgement keeps its plain wording")

    async def test_an_issue_with_no_alerts_has_none_in_its_log(self):
        self.assertFalse(any("Alert" in t for t in await self.log()))


if __name__ == "__main__":
    unittest.main()
