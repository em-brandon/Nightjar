"""Daily rollup, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_rollup -v

What matters: a finished day is summed correctly, today is never rolled up, running it twice changes
nothing, and the prune step never deletes checks it has not rolled up first.
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, pool, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate, probe  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import Check, Client, DailyStat, Event, Issue, Notification, Site  # noqa: E402
from hub.rollup import rollup  # noqa: E402

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
D1, D2, TODAY = date(2026, 3, 8), date(2026, 3, 9), date(2026, 3, 10)


def at(day: date, hour: int = 10) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


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
class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    """A migrated throwaway database per class, emptied and re-seeded with one client and two sites per test."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_roll_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        self.engine = create_async_engine(self.url, poolclass=pool.NullPool)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites RESTART IDENTITY CASCADE"))
        async with self.maker() as s:
            client = Client(name="Bird Box")
            s.add(client)
            await s.flush()
            self.site = Site(client_id=client.id, name="bird-box")
            self.quiet = Site(client_id=client.id, name="no-activity")
            s.add_all([self.site, self.quiet])
            await s.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def rows(self):
        async with self.maker() as s:
            result = await s.execute(select(DailyStat).order_by(DailyStat.site_id, DailyStat.day))
            return result.scalars().all()


class Rollup(DatabaseCase):
    async def seed_day_one(self, s):
        """Five good checks (100..500 ms) and one failure, three events, one new issue."""
        for ms in (100, 200, 300, 400, 500):
            s.add(Check(site_id=self.site.id, checked_at=at(D1, 9), ok=True, status_code=200, latency_ms=ms))
        s.add(Check(site_id=self.site.id, checked_at=at(D1, 9), ok=False, error="ReadTimeout"))
        issue = Issue(site_id=self.site.id, fingerprint="f1", title="boom", first_seen=at(D1), last_seen=at(D1))
        s.add(issue)
        await s.flush()
        for hour in (1, 2, 3):
            s.add(Event(issue_id=issue.id, payload={}, occurred_at=at(D1, hour)))
        return issue

    async def test_a_finished_day_is_summed_and_today_is_left_out(self):
        async with self.maker() as s:
            await self.seed_day_one(s)
            s.add(Check(site_id=self.site.id, checked_at=at(TODAY, 8), ok=True, status_code=200, latency_ms=50))
            await s.commit()
        async with self.maker() as s:
            written = await rollup(s, NOW)

        self.assertEqual(written, 1)
        (row,) = await self.rows()
        self.assertEqual((row.site_id, row.day), (self.site.id, D1))
        self.assertEqual((row.checks_total, row.checks_ok), (6, 5))
        self.assertEqual(row.latency_avg_ms, 300)
        self.assertEqual(row.latency_p95_ms, 480)  # 95th percentile of 100..500, interpolated
        self.assertEqual((row.events, row.issues_opened), (3, 1))

    async def test_alerts_are_counted_on_the_day_they_were_raised(self):
        async with self.maker() as s:
            issue = await self.seed_day_one(s)
            for i, (status, expired) in enumerate([("sent", None), ("sent", at(D2, 11)), ("failed", None)]):
                s.add(
                    Notification(
                        issue_id=issue.id,
                        change="spike",
                        changed_at=at(D2, i),
                        severity="P1",
                        priority=2,
                        site="bird-box",
                        title="boom",
                        status=status,
                        created_at=at(D2, i),
                        expired_at=expired,
                    )
                )
            await s.commit()
        async with self.maker() as s:
            await rollup(s, NOW)

        by_day = {r.day: r for r in await self.rows()}
        self.assertEqual(set(by_day), {D1, D2})
        d2 = by_day[D2]
        self.assertEqual((d2.alerts_sent, d2.alerts_failed, d2.p1_expired), (2, 1, 1))
        self.assertEqual((d2.checks_total, d2.events, d2.issues_opened), (0, 0, 0))
        self.assertIsNone(d2.latency_avg_ms)

    async def test_a_day_of_only_failed_checks_has_no_latency_not_zero(self):
        async with self.maker() as s:
            s.add_all(
                [Check(site_id=self.site.id, checked_at=at(D1, h), ok=False, error="ConnectError") for h in (1, 2)]
            )
            await s.commit()
        async with self.maker() as s:
            await rollup(s, NOW)
        (row,) = await self.rows()
        self.assertEqual((row.checks_total, row.checks_ok), (2, 0))
        self.assertIsNone(row.latency_avg_ms)
        self.assertIsNone(row.latency_p95_ms)

    async def test_a_site_with_nothing_recorded_gets_no_row(self):
        async with self.maker() as s:
            await self.seed_day_one(s)
            await s.commit()
        async with self.maker() as s:
            await rollup(s, NOW)
        self.assertEqual({r.site_id for r in await self.rows()}, {self.site.id})

    async def test_nothing_recorded_writes_nothing(self):
        async with self.maker() as s:
            self.assertEqual(await rollup(s, NOW), 0)
        self.assertEqual(await self.rows(), [])

    async def test_running_twice_changes_nothing(self):
        async with self.maker() as s:
            await self.seed_day_one(s)
            await s.commit()
        async with self.maker() as s:
            await rollup(s, NOW)
        first = [(r.site_id, r.day, r.checks_total, r.events, r.latency_p95_ms) for r in await self.rows()]
        async with self.maker() as s:
            await rollup(s, NOW)
        second = [(r.site_id, r.day, r.checks_total, r.events, r.latency_p95_ms) for r in await self.rows()]
        self.assertEqual(first, second)
        async with self.maker() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(DailyStat)), 1)

    async def test_a_later_run_adds_the_new_day_and_repairs_the_last_one(self):
        async with self.maker() as s:
            await self.seed_day_one(s)
            await s.commit()
        async with self.maker() as s:
            await rollup(s, NOW - timedelta(days=1))  # D1 is done, D2 is "today"
        self.assertEqual({r.day for r in await self.rows()}, {D1})

        async with self.maker() as s:
            s.add(Check(site_id=self.site.id, checked_at=at(D1, 23), ok=True, status_code=200, latency_ms=100))
            s.add(Check(site_id=self.site.id, checked_at=at(D2, 5), ok=True, status_code=200, latency_ms=100))
            await s.commit()
        async with self.maker() as s:
            await rollup(s, NOW)

        by_day = {r.day: r for r in await self.rows()}
        self.assertEqual(set(by_day), {D1, D2})
        self.assertEqual(by_day[D1].checks_total, 7)  # the late check on D1 was picked up
        self.assertEqual(by_day[D2].checks_total, 1)

    async def test_deleting_a_site_deletes_its_history(self):
        async with self.maker() as s:
            await self.seed_day_one(s)
            await s.commit()
        async with self.maker() as s:
            await rollup(s, NOW)
        async with self.maker() as s:
            await s.execute(text("DELETE FROM sites WHERE id = :i"), {"i": self.site.id})
            await s.commit()
        self.assertEqual(await self.rows(), [])


class PruneKeepsHistory(DatabaseCase):
    """The prune step must never delete a check it has not rolled up."""

    async def add_old_check(self):
        old = datetime.now(UTC) - probe.RETENTION - timedelta(days=3)
        async with self.maker() as s:
            s.add(Check(site_id=self.site.id, checked_at=old, ok=True, status_code=200, latency_ms=120))
            await s.commit()
        return old.date()

    async def count_checks(self):
        async with self.maker() as s:
            return await s.scalar(select(func.count()).select_from(Check))

    async def test_old_checks_are_rolled_up_and_then_deleted(self):
        day = await self.add_old_check()
        with patch.object(probe, "SessionLocal", self.maker):
            await probe._prune()
        self.assertEqual(await self.count_checks(), 0)
        (row,) = await self.rows()
        self.assertEqual((row.day, row.checks_total, row.latency_avg_ms), (day, 1, 120))

    async def test_a_failed_rollup_keeps_the_checks(self):
        await self.add_old_check()
        with patch.object(probe, "SessionLocal", self.maker), patch.object(
            probe, "rollup", side_effect=RuntimeError("boom")
        ):
            await probe._prune()
        self.assertEqual(await self.count_checks(), 1)
        self.assertEqual(await self.rows(), [])


if __name__ == "__main__":
    unittest.main()
