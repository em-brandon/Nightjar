"""Availability reports (hub/reports.py): the arithmetic, and the endpoint against a real database.

    python -m unittest tests.test_reports -v

The pure functions need no database. The endpoint tests run against a throwaway migrated database and are skipped, by
name, when Postgres is not reachable.
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate, reports  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402
from hub.models import Check, Client, DailyStat, Deploy, Site  # noqa: E402
from tests.test_auth import _admin, _reachable  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


class Months(unittest.TestCase):
    def test_a_month_is_utc_and_its_end_is_the_first_of_the_next(self):
        start, end, partial = reports.month_bounds("2026-10", NOW)
        self.assertEqual((start, end, partial), (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC), False))

    def test_december_rolls_into_the_next_year(self):
        start, end, _ = reports.month_bounds("2025-12", NOW)
        self.assertEqual(end, datetime(2026, 1, 1, tzinfo=UTC))

    def test_the_current_month_is_partial_and_is_the_default(self):
        start, end, partial = reports.month_bounds(None, NOW)
        self.assertEqual(start, datetime(2026, 11, 1, tzinfo=UTC))
        self.assertTrue(partial)

    def test_bad_future_and_ancient_months_are_refused_with_a_reason(self):
        for bad in ("2026-13", "2026-00", "26-10", "2026-1", "latest", "2026-10-01", "2026-12", "2020-01"):
            with self.subTest(month=bad), self.assertRaises(ValueError):
                reports.month_bounds(bad, NOW)


class Percentages(unittest.TestCase):
    def test_no_checks_is_no_number_not_a_perfect_one(self):
        self.assertIsNone(reports.uptime_pct(0, 0))

    def test_every_check_passing_is_exactly_one_hundred(self):
        self.assertEqual(reports.uptime_pct(1440, 1440), 100.0)

    def test_one_failure_never_rounds_up_to_one_hundred(self):
        # 1 failure in 43,200 checks (a month at 60 s) is 99.9977%: it must not be shown as 100.00
        self.assertEqual(reports.uptime_pct(43_199, 43_200), 99.99)
        self.assertLess(reports.uptime_pct(999_999, 1_000_000), 100.0)

    def test_it_truncates_rather_than_rounds(self):
        self.assertEqual(reports.uptime_pct(99_955, 100_000), 99.95)
        self.assertEqual(reports.uptime_pct(2, 3), 66.66)

    def test_downtime_is_failed_checks_times_the_interval(self):
        self.assertEqual(reports.downtime_minutes(0, 60), 0)
        self.assertEqual(reports.downtime_minutes(45, 60), 45)
        self.assertEqual(reports.downtime_minutes(3, 300), 15)

    def test_coverage_says_how_much_of_the_time_the_probe_was_checking(self):
        first = date(2026, 11, 1)
        through = datetime(2026, 11, 2, tzinfo=UTC)  # one day = 1,440 checks at 60 s
        self.assertEqual(reports.coverage_pct(1440, first, through, 60), 100.0)
        self.assertEqual(reports.coverage_pct(720, first, through, 60), 50.0)
        self.assertEqual(reports.coverage_pct(5000, first, through, 60), 100.0, "never over 100")
        self.assertIsNone(reports.coverage_pct(0, None, through, 60))


class Headlines(unittest.TestCase):
    def test_nothing_measured_says_so_instead_of_claiming_availability(self):
        verdict, text_ = reports.headline([("shop", None)], 99.9, False)
        self.assertEqual(verdict, "no_data")
        self.assertIn("no availability data", text_)

    def test_perfect_met_and_below(self):
        self.assertEqual(reports.headline([("a", 100.0), ("b", 100.0)], 99.9, False)[0], "perfect")
        self.assertEqual(reports.headline([("a", 100.0), ("b", 99.95)], 99.9, False)[0], "met")
        verdict, text_ = reports.headline([("a", 100.0), ("shop", 99.2)], 99.9, False)
        self.assertEqual(verdict, "below")
        self.assertIn("shop (99.2%)", text_)
        self.assertNotIn("a (", text_, "a site that met the target is not named as a problem")

    def test_one_site_is_worded_as_one_site(self):
        self.assertTrue(reports.headline([("a", 100.0)], 99.9, False)[1].startswith("The site was"))

    def test_incomplete_monitoring_is_stated_even_when_everything_passed(self):
        self.assertIn("incomplete", reports.headline([("a", 100.0)], 99.9, True)[1])

    def test_an_unmeasured_site_is_not_counted_as_meeting_the_target(self):
        verdict, _ = reports.headline([("a", 100.0), ("b", None)], 99.9, False)
        self.assertEqual(verdict, "perfect", "the measured one decides; the other is listed elsewhere, not made up")


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Endpoint(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_rep_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for p in (patch.object(settings, "console_token", ""), patch.object(settings, "probe_interval_s", 60)):
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, poolclass=pool.NullPool)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, checks, daily_stats, deploys RESTART IDENTITY CASCADE"))

    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_session, None)
        await self.engine.dispose()

    # helpers -------------------------------------------------------------------------------------------------
    async def seed_site(self, client="Acme", name="acme-web", env="production", url="https://acme.example/health?token=SECRET"):
        async with self.maker() as db:
            c = await db.scalar(text("SELECT id FROM clients WHERE name = :n"), {"n": client})
            if c is None:
                obj = Client(name=client, contact_email="owner@acme.example")
                db.add(obj)
                await db.flush()
                c = obj.id
            site = Site(client_id=c, name=name, env=env, url=url)
            db.add(site)
            await db.commit()
            return site.id

    async def seed_checks(self, site_id, start: datetime, pattern: list[bool], latency=120):
        """One check a minute from `start`, following `pattern` (True = passed)."""
        async with self.maker() as db:
            for i, ok in enumerate(pattern):
                db.add(Check(site_id=site_id, checked_at=start + timedelta(minutes=i), ok=ok, latency_ms=latency if ok else None))
            await db.commit()

    async def get(self, month=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.get("/api/reports/availability", params={"month": month} if month else None)

    def last_month(self):
        first = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return first - timedelta(days=1)

    # tests ---------------------------------------------------------------------------------------------------
    async def test_a_month_with_one_outage_gives_the_percentage_the_downtime_and_the_incident(self):
        sid = await self.seed_site()
        lm = self.last_month().replace(day=3, hour=10, minute=0)
        # 600 checks: 10 failed in a row in the middle, the rest passed
        await self.seed_checks(sid, lm, [True] * 300 + [False] * 10 + [True] * 290)
        r = await self.get(lm.strftime("%Y-%m"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        site = body["clients"][0]["sites"][0]
        self.assertEqual(site["checks_total"], 600)
        self.assertEqual(site["uptime_pct"], 98.33)
        self.assertEqual(site["downtime_minutes"], 10)
        self.assertEqual(len(site["incidents"]), 1)
        inc = site["incidents"][0]
        self.assertEqual(inc["minutes"], 10)
        self.assertFalse(inc["ongoing"])
        self.assertEqual(site["met_target"], False)
        self.assertEqual(body["clients"][0]["verdict"], "below")

    async def test_a_single_failed_check_counts_toward_the_total_but_is_not_an_incident(self):
        sid = await self.seed_site()
        lm = self.last_month().replace(day=3, hour=10, minute=0)
        await self.seed_checks(sid, lm, [True] * 100 + [False] + [True] * 100)
        site = (await self.get(lm.strftime("%Y-%m"))).json()["clients"][0]["sites"][0]
        self.assertEqual(site["incidents"], [])
        self.assertEqual(site["downtime_minutes"], 1)
        self.assertLess(site["uptime_pct"], 100.0)

    async def test_failures_either_side_of_a_gap_in_probing_are_not_one_long_outage(self):
        sid = await self.seed_site()
        lm = self.last_month().replace(day=3, hour=10, minute=0)
        # two failures, then the probe is off for five hours, then two more: that is two short sightings, not 5 hours down
        await self.seed_checks(sid, lm, [True] * 20 + [False] * 2)
        await self.seed_checks(sid, lm + timedelta(hours=5), [False] * 2 + [True] * 20)
        site = (await self.get(lm.strftime("%Y-%m"))).json()["clients"][0]["sites"][0]
        self.assertEqual(len(site["incidents"]), 2)
        self.assertEqual(sorted(i["minutes"] for i in site["incidents"]), [2, 2])

    async def test_a_site_that_was_never_checked_has_no_number_and_is_not_called_available(self):
        await self.seed_site()
        body = (await self.get()).json()
        site = body["clients"][0]["sites"][0]
        self.assertIsNone(site["uptime_pct"])
        self.assertIsNone(site["met_target"])
        self.assertEqual(body["clients"][0]["verdict"], "no_data")
        self.assertIsNone(body["overall_uptime_pct"])

    async def test_staging_and_unmonitored_sites_are_not_in_the_numbers(self):
        await self.seed_site()
        await self.seed_site(name="acme-web-staging", env="staging")
        await self.seed_site(name="acme-intranet", url=None)
        client = (await self.get()).json()["clients"][0]
        self.assertEqual([s["name"] for s in client["sites"]], ["acme-web"])
        self.assertEqual(client["not_monitored"], ["acme-intranet"])

    async def test_only_the_host_of_a_site_is_shown_never_its_query_string(self):
        await self.seed_site()
        raw = (await self.get()).text
        self.assertIn("acme.example", raw)
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("token=", raw)

    async def test_a_rolled_up_day_is_used_when_the_raw_checks_are_gone(self):
        sid = await self.seed_site()
        lm = self.last_month()
        day = lm.replace(day=5).date()
        async with self.maker() as db:
            db.add(DailyStat(site_id=sid, day=day, checks_total=1440, checks_ok=1430, latency_avg_ms=200, latency_p95_ms=450))
            await db.commit()
        site = (await self.get(lm.strftime("%Y-%m"))).json()["clients"][0]["sites"][0]
        self.assertEqual(site["checks_total"], 1440)
        self.assertEqual(site["downtime_minutes"], 10)
        self.assertEqual(site["latency_avg_ms"], 200)
        self.assertEqual(site["latency_peak_p95_ms"], 450)

    async def test_a_rolled_up_day_wins_over_raw_rows_for_the_same_day(self):
        sid = await self.seed_site()
        lm = self.last_month().replace(day=5, hour=1, minute=0)
        await self.seed_checks(sid, lm, [True] * 10)  # raw: 10 checks
        async with self.maker() as db:
            db.add(DailyStat(site_id=sid, day=lm.date(), checks_total=1440, checks_ok=1440, latency_avg_ms=100, latency_p95_ms=100))
            await db.commit()
        site = (await self.get(lm.strftime("%Y-%m"))).json()["clients"][0]["sites"][0]
        self.assertEqual(site["checks_total"], 1440, "counted once, from the rollup, not 1440 + 10")

    async def test_coverage_drops_when_the_probe_was_not_running(self):
        sid = await self.seed_site()
        lm = self.last_month().replace(day=5, hour=0, minute=0)
        async with self.maker() as db:
            db.add(DailyStat(site_id=sid, day=lm.date(), checks_total=300, checks_ok=300, latency_avg_ms=100, latency_p95_ms=100))
            db.add(DailyStat(site_id=sid, day=(lm + timedelta(days=1)).date(), checks_total=1440, checks_ok=1440, latency_avg_ms=100, latency_p95_ms=100))
            await db.commit()
        client = (await self.get(lm.strftime("%Y-%m"))).json()["clients"][0]
        site = client["sites"][0]
        self.assertEqual(site["uptime_pct"], 100.0)
        self.assertLess(site["coverage_pct"], 90.0)
        self.assertIn("incomplete", client["headline"], "a perfect number over a gap must say there was a gap")

    async def test_deploys_are_counted_by_outcome_for_the_month(self):
        sid = await self.seed_site()
        lm = self.last_month().replace(day=9, hour=9)
        async with self.maker() as db:
            for status in ("success", "success", "failed", "running"):
                db.add(Deploy(site_id=sid, ref="main@abc", status=status, created_at=lm))
            await db.commit()
        site = (await self.get(lm.strftime("%Y-%m"))).json()["clients"][0]["sites"][0]
        self.assertEqual((site["deploys"], site["failed_deploys"]), (2, 1))

    async def test_an_old_month_keeps_its_totals_but_says_the_outage_list_is_not_kept(self):
        sid = await self.seed_site()
        old = (datetime.now(UTC) - timedelta(days=150)).replace(day=10)
        async with self.maker() as db:
            db.add(DailyStat(site_id=sid, day=old.date(), checks_total=1440, checks_ok=1400, latency_avg_ms=100, latency_p95_ms=100))
            await db.commit()
        site = (await self.get(old.strftime("%Y-%m"))).json()["clients"][0]["sites"][0]
        self.assertEqual(site["downtime_minutes"], 40)
        self.assertFalse(site["incident_detail"])

    async def test_clients_are_listed_by_name_and_each_gets_only_its_own_sites(self):
        await self.seed_site(client="Zulu", name="zulu-web", url="https://zulu.example")
        await self.seed_site(client="Acme", name="acme-web")
        clients = (await self.get()).json()["clients"]
        self.assertEqual([c["name"] for c in clients], ["Acme", "Zulu"])
        self.assertEqual([s["name"] for s in clients[1]["sites"]], ["zulu-web"])

    async def test_a_bad_month_is_a_422_with_a_reason(self):
        r = await self.get("2026-13")
        self.assertEqual(r.status_code, 422)
        self.assertIn("month", r.json()["detail"])

    async def test_the_route_needs_the_console_token_like_every_other(self):
        with patch.object(settings, "console_token", "correct-horse-battery-staple-1234"), patch.object(settings, "operator_login", False):
            self.assertEqual((await self.get()).status_code, 401)


if __name__ == "__main__":
    unittest.main()
