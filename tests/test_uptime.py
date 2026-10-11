"""A site that stops answering becomes an issue and an alert, and resolves itself when it recovers.
The rules are pure; the behaviour runs against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_uptime -v
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import pool, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate, uptime  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import Check, Client, Issue, IssueAction, Notification, Site  # noqa: E402

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


class Rules(unittest.TestCase):
    """`oks` is newest first, the order the probe's recent checks come in."""

    def test_an_outage_starts_at_the_second_failure_in_a_row(self):
        self.assertFalse(uptime.outage_started([]))
        self.assertFalse(uptime.outage_started([False]), "one failed probe is only a warning")
        self.assertTrue(uptime.outage_started([False, False]))
        self.assertTrue(uptime.outage_started([False, False, True, True]))

    def test_an_outage_that_is_already_under_way_does_not_start_again(self):
        self.assertFalse(uptime.outage_started([False, False, False]))
        self.assertFalse(uptime.outage_started([False, False, False, True]))

    def test_a_single_blip_between_passes_is_not_an_outage(self):
        self.assertFalse(uptime.outage_started([True, False, True]))
        self.assertFalse(uptime.outage_started([False, True, False]))

    def test_recovery_needs_a_run_of_passes_not_one(self):
        self.assertFalse(uptime.recovered([True, True, True, True, False], 5))
        self.assertTrue(uptime.recovered([True] * 5, 5))
        self.assertTrue(uptime.recovered([True] * 7 + [False], 5))
        self.assertFalse(uptime.recovered([True] * 4, 5), "not enough history to be sure")

    def test_the_event_is_fatal_for_production_and_an_error_for_staging(self):
        prod = uptime.make_event(Site(env="production", name="a"), "HTTP 503")
        stage = uptime.make_event(Site(env="staging", name="a-staging"), None)
        self.assertEqual((prod.level, stage.level), ("fatal", "error"))
        self.assertEqual(prod.message, stage.message, "one fixed message, so one outage is one issue")
        self.assertNotIn("503", prod.message)
        self.assertEqual(prod.extra, {"last_error": "HTTP 503"})


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
class Behaviour(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_uptime_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        p = patch.object(settings, "uptime_recover_checks", 5)
        p.start()
        self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, issues, events, checks, notifications, issue_actions RESTART IDENTITY CASCADE"))
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            prod = Site(client_id=client.id, name="acme-shop", env="production", url="https://acme.example")
            stage = Site(client_id=client.id, name="acme-shop-staging", env="staging", url="https://stage.example")
            session.add_all([prod, stage])
            await session.commit()
            self.prod_id, self.stage_id = prod.id, stage.id
        self.clock = T0

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def probe(self, site_id, *oks, error="HTTP 503"):
        """Record one check per value, then run the reconcile step the probe runs after each round."""
        results = []
        for ok in oks:
            self.clock += timedelta(minutes=1)
            async with self.maker() as session:
                session.add(Check(site_id=site_id, checked_at=self.clock, ok=ok, status_code=200 if ok else 503, error=None if ok else error))
                await session.commit()
                site = await session.get(Site, site_id)
                results.append(await uptime.reconcile_site(session, site))
        return results

    async def issues(self):
        async with self.maker() as session:
            return list((await session.scalars(select(Issue).order_by(Issue.id))).all())

    async def alerts(self):
        async with self.maker() as session:
            return list((await session.scalars(select(Notification).order_by(Notification.id))).all())

    async def test_one_failed_check_opens_nothing(self):
        self.assertEqual(await self.probe(self.prod_id, False), [None])
        self.assertEqual(await self.issues(), [])

    async def test_the_second_failed_check_opens_a_p1_and_queues_an_alert_that_wakes_the_phone(self):
        results = await self.probe(self.prod_id, False, False)
        self.assertEqual(results, [None, "opened"])
        (issue,) = await self.issues()
        self.assertEqual((issue.severity, issue.status, issue.title), ("P1", "open", "UptimeCheckFailed: the site is not responding to uptime checks"))
        (alert,) = await self.alerts()
        self.assertEqual((alert.severity, alert.priority, alert.change, alert.site), ("P1", 2, "new", "acme-shop"))

    async def test_a_long_outage_is_one_issue_and_one_alert_not_one_per_check(self):
        await self.probe(self.prod_id, *[False] * 30)
        (issue,) = await self.issues()
        self.assertEqual(issue.event_count, 1, "edge-triggered: only the start of an outage is an event")
        self.assertEqual(len(await self.alerts()), 1)

    async def test_a_staging_outage_is_a_p2_not_a_wake_up(self):
        await self.probe(self.stage_id, False, False)
        (issue,) = await self.issues()
        self.assertEqual(issue.severity, "P2")
        (alert,) = await self.alerts()
        self.assertNotEqual(alert.priority, 2)

    async def test_it_resolves_itself_only_after_a_run_of_passing_checks(self):
        await self.probe(self.prod_id, False, False)
        early = await self.probe(self.prod_id, True, True, True, True)
        self.assertEqual(early, [None] * 4)
        self.assertEqual((await self.issues())[0].status, "open", "four passes is not enough")
        self.assertEqual(await self.probe(self.prod_id, True), ["resolved"])
        issue = (await self.issues())[0]
        self.assertEqual(issue.status, "resolved")
        async with self.maker() as session:
            action = await session.scalar(select(IssueAction).where(IssueAction.issue_id == issue.id, IssueAction.action == "resolve"))
        self.assertTrue(action.detail.startswith("auto:"), "a resolve by the hub says so")

    async def test_a_flapping_site_does_not_resolve_between_blips(self):
        await self.probe(self.prod_id, False, False)
        await self.probe(self.prod_id, True, True, False, True, True)
        self.assertEqual((await self.issues())[0].status, "open")

    async def test_a_second_outage_after_recovery_is_a_regression_and_alerts_again(self):
        await self.probe(self.prod_id, False, False)
        await self.probe(self.prod_id, *[True] * 5)
        self.assertEqual((await self.issues())[0].status, "resolved")
        await self.probe(self.prod_id, False, False)
        self.assertEqual([a.change for a in await self.alerts()], ["new", "regression"])
        self.assertEqual((await self.issues())[0].status, "open")

    async def test_a_muted_uptime_issue_stays_quiet_through_a_new_outage(self):
        await self.probe(self.prod_id, False, False)
        async with self.maker() as session:
            issue = (await session.scalars(select(Issue))).one()
            issue.status = "muted"
            await session.commit()
        await self.probe(self.prod_id, *[True] * 5)
        await self.probe(self.prod_id, False, False)
        self.assertEqual(len(await self.alerts()), 1, "only the original: a muted issue never speaks again")
        self.assertEqual((await self.issues())[0].status, "muted")

    async def test_a_site_with_no_checks_does_nothing(self):
        async with self.maker() as session:
            site = await session.get(Site, self.prod_id)
            self.assertIsNone(await uptime.reconcile_site(session, site))

    async def test_a_failure_for_one_site_does_not_stop_the_others(self):
        await self.probe(self.stage_id, False, False)
        async with self.maker() as session:
            real = uptime.reconcile_site
            seen = []

            async def flaky(s, site):
                seen.append(site.id)
                if site.id == self.prod_id:
                    raise RuntimeError("boom")
                return await real(s, site)

            with patch.object(uptime, "reconcile_site", flaky):
                await uptime.reconcile(session, [self.prod_id, self.stage_id])
        self.assertEqual(seen, [self.prod_id, self.stage_id], "the second site was still reconciled")


if __name__ == "__main__":
    unittest.main()
