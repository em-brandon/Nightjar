"""Writing to the alert outbox, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_outbox -v
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
from sqlalchemy import pool, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate, outbox  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402
from hub.models import Client, Issue, Notification, Presence, Site  # noqa: E402

DAY = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)  # 12:00 in Nairobi
NIGHT = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)  # 23:00 in Nairobi


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


class WindowStart(unittest.TestCase):
    def test_it_floors_to_the_clock_not_to_the_event(self):
        t = datetime(2026, 10, 12, 9, 37, 12, tzinfo=UTC)
        self.assertEqual(outbox.window_start(t, 10), datetime(2026, 10, 12, 9, 30, tzinfo=UTC))
        self.assertEqual(outbox.window_start(datetime(2026, 10, 12, 9, 30, tzinfo=UTC), 10), datetime(2026, 10, 12, 9, 30, tzinfo=UTC))

    def test_events_in_one_window_share_a_start(self):
        a = outbox.window_start(datetime(2026, 10, 12, 9, 30, 1, tzinfo=UTC), 10)
        b = outbox.window_start(datetime(2026, 10, 12, 9, 39, 59, tzinfo=UTC), 10)
        c = outbox.window_start(datetime(2026, 10, 12, 9, 40, 0, tzinfo=UTC), 10)
        self.assertEqual(a, b)
        self.assertNotEqual(b, c)


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Enqueue(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_outbox_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, issues, notifications, presence RESTART IDENTITY CASCADE"))

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def issue(self, session, severity="P2", status="open", title="Boom: it broke") -> Issue:
        client = Client(name="Acme")
        session.add(client)
        await session.flush()
        site = Site(client_id=client.id, name="acme-shop")
        session.add(site)
        await session.flush()
        issue = Issue(site_id=site.id, fingerprint=uuid.uuid4().hex, title=title, severity=severity, status=status)
        session.add(issue)
        await session.flush()
        return issue

    async def rows(self) -> list[Notification]:
        async with self.maker() as session:
            return list((await session.scalars(select(Notification).order_by(Notification.id))).all())

    async def write(self, change="new", severity="P2", status="open", now=DAY, changed_at=None, issue=None):
        async with self.maker() as session:
            issue = issue or await self.issue(session, severity, status)
            wrote = await outbox.enqueue(session, issue, "acme-shop", change, changed_at or now, now)
            await session.commit()
            return wrote, issue

    async def test_a_new_p2_writes_one_pending_row_with_the_policys_answer(self):
        wrote, issue = await self.write()
        self.assertTrue(wrote)
        (row,) = await self.rows()
        self.assertEqual((row.issue_id, row.change, row.severity, row.status, row.attempts), (issue.id, "new", "P2", "pending", 0))
        self.assertEqual((row.priority, row.speaker), (0, None))
        self.assertEqual((row.site, row.title), ("acme-shop", "Boom: it broke"))
        self.assertIsNone(row.sent_at)

    async def test_a_p1_is_priority_two(self):
        await self.write(severity="P1")
        (row,) = await self.rows()
        self.assertEqual(row.priority, 2)

    async def test_a_p2_at_night_is_queued_quiet_not_dropped(self):
        wrote, _ = await self.write(now=NIGHT)
        self.assertTrue(wrote)
        (row,) = await self.rows()
        self.assertEqual(row.priority, -1)

    async def test_presence_home_adds_the_speaker_and_no_record_means_away(self):
        await self.write(severity="P1")
        self.assertIsNone((await self.rows())[0].speaker, "no presence record is away")
        async with self.maker() as session:
            session.add(Presence(state="home", source="nfc", expires_at=DAY + timedelta(hours=14)))
            await session.commit()
        await self.write(severity="P1", now=DAY + timedelta(minutes=1))
        self.assertEqual((await self.rows())[1].speaker, "alarm")

    async def test_an_expired_home_tap_counts_as_away(self):
        async with self.maker() as session:
            session.add(Presence(state="home", source="nfc", expires_at=DAY - timedelta(minutes=1)))
            await session.commit()
        await self.write(severity="P1")
        self.assertIsNone((await self.rows())[0].speaker)

    async def test_a_p3_writes_no_row_because_it_goes_to_the_digest(self):
        wrote, _ = await self.write(severity="P3")
        self.assertFalse(wrote)
        self.assertEqual(await self.rows(), [])

    async def test_a_muted_issue_writes_no_row_even_on_regression(self):
        for change in ("new", "regression", "spike"):
            wrote, _ = await self.write(change=change, status="muted")
            self.assertFalse(wrote, change)
        self.assertEqual(await self.rows(), [])

    async def test_no_state_change_writes_no_row(self):
        wrote, _ = await self.write(change=None)
        self.assertFalse(wrote)
        self.assertEqual(await self.rows(), [])

    async def test_the_same_change_twice_is_one_row(self):
        wrote, issue = await self.write()
        again, _ = await self.write(issue=issue)
        self.assertTrue(wrote)
        self.assertFalse(again, "a retried ingest must not write a second row")
        self.assertEqual(len(await self.rows()), 1)

    async def test_a_different_change_or_time_is_a_new_row(self):
        _, issue = await self.write(change="new", changed_at=DAY)
        await self.write(change="regression", changed_at=DAY, issue=issue)
        await self.write(change="regression", changed_at=DAY + timedelta(hours=1), issue=issue)
        self.assertEqual([r.change for r in await self.rows()], ["new", "regression", "regression"])


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class ThroughIngest(unittest.IsolatedAsyncioTestCase):
    """The same rules, driven through the real ingest path."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_outbox_ing_{uuid.uuid4().hex[:8]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for name, value in (("verify_signatures", False), ("console_token", ""), ("spike_threshold", 50)):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, issues, events, notifications, presence RESTART IDENTITY CASCADE"))
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            site = Site(client_id=client.id, name="acme-shop")
            session.add(site)
            await session.commit()
            self.key = site.ingest_key
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def crash(self, message="cart is empty", level="error", type_="KeyError"):
        r = await self.http.post(f"/ingest/{self.key}/event", json={"exception_type": type_, "message": message, "level": level})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def rows(self):
        async with self.maker() as session:
            return list((await session.scalars(select(Notification).order_by(Notification.id))).all())

    async def test_a_new_crash_writes_one_notification(self):
        out = await self.crash()
        self.assertEqual(out["state_change"], "new")
        (row,) = await self.rows()
        self.assertEqual((row.issue_id, row.change, row.status, row.severity), (out["issue_id"], "new", "pending", "P2"))

    async def test_the_same_crash_again_writes_no_second_row(self):
        await self.crash()
        again = await self.crash()
        self.assertIsNone(again["state_change"])
        self.assertEqual(again["event_count"], 2)
        self.assertEqual(len(await self.rows()), 1)

    async def test_two_hundred_identical_crashes_are_one_issue_and_one_notification(self):
        # the spike threshold is raised so only the dedupe is under test
        with patch.object(settings, "spike_threshold", 10_000):
            ids = {(await self.crash())["issue_id"] for _ in range(200)}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(await self.rows()), 1)

    async def test_two_hundred_crashes_past_the_spike_threshold_are_a_few_notifications_not_two_hundred(self):
        ids = {(await self.crash())["issue_id"] for _ in range(200)}
        self.assertEqual(len(ids), 1)
        changes = [r.change for r in await self.rows()]
        self.assertEqual(changes[0], "new")
        # the 51st event crosses the threshold: one spike alert for its window (two if the clock ticked over)
        self.assertTrue(1 <= changes.count("spike") <= 2, changes)
        self.assertLessEqual(len(changes), 3)

    async def test_a_fatal_is_priority_two_and_a_warning_writes_nothing(self):
        await self.crash(level="fatal", message="the process died")
        await self.crash(level="warning", message="a heads up")
        rows = await self.rows()
        self.assertEqual([(r.severity, r.priority) for r in rows], [("P1", 2)])

    async def test_a_resolved_issue_that_comes_back_is_a_second_notification(self):
        out = await self.crash()
        async with self.maker() as session:
            issue = await session.get(Issue, out["issue_id"])
            issue.status = "resolved"
            await session.commit()
        back = await self.crash()
        self.assertEqual(back["state_change"], "regression")
        self.assertEqual([r.change for r in await self.rows()], ["new", "regression"])

    async def test_a_muted_issue_that_comes_back_writes_nothing(self):
        out = await self.crash()
        async with self.maker() as session:
            issue = await session.get(Issue, out["issue_id"])
            issue.status = "muted"
            await session.commit()
        again = await self.crash()
        self.assertIsNone(again["state_change"])
        self.assertEqual(len(await self.rows()), 1, "only the original")

    async def test_a_spike_is_one_notification_per_window_not_one_per_event(self):
        with patch.object(settings, "spike_threshold", 3):
            states = [(await self.crash())["state_change"] for _ in range(10)]
        self.assertEqual(states[0], "new")
        self.assertIn("spike", states)
        # a spike can straddle two ten-minute windows if the clock ticks over mid-test, but never more
        changes = [r.change for r in await self.rows()]
        self.assertEqual(changes[0], "new")
        self.assertTrue(1 <= changes.count("spike") <= 2, changes)

    async def test_the_event_and_its_notification_stand_or_fall_together(self):
        with patch.object(outbox, "enqueue", side_effect=RuntimeError("the outbox is broken")):
            with self.assertRaises(RuntimeError):
                await self.crash()
        async with self.maker() as session:
            issues = (await session.scalars(select(Issue))).all()
            events = (await session.execute(text("SELECT count(*) FROM events"))).scalar()
        self.assertEqual((len(issues), events), (0, 0), "a crash that could not be queued must not be half-recorded")
        self.assertEqual(await self.rows(), [])


if __name__ == "__main__":
    unittest.main()
