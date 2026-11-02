"""Heartbeats: a thing that stops pinging becomes an issue, once, and resolves itself when the pings return.
The rules are pure; the behaviour runs against a real throwaway database with a clock the test controls.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_heartbeat_checks -v

(tests/test_heartbeat.py is a different thing: the dead-man's switch that watches the alert sender.)
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

from hub import heartbeats, migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402
from hub.models import Client, Heartbeat, Issue, IssueAction, Notification, Site  # noqa: E402
from hub.schemas import HeartbeatIn  # noqa: E402

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
KEY = "hb-key"


class Rules(unittest.TestCase):
    def hb(self, last_ping_at=T0, interval_s=300, grace_s=60, last_status="ok", **flags):
        return Heartbeat(
            name="nightly", kind="job", interval_s=interval_s, grace_s=grace_s, last_ping_at=last_ping_at,
            last_status=last_status,
            missed_open=flags.get("missed_open", False), failed_open=flags.get("failed_open", False),
        )

    def test_grace_is_a_fifth_of_the_interval_between_a_minute_and_an_hour(self):
        self.assertEqual(heartbeats.default_grace(30), 60)
        self.assertEqual(heartbeats.default_grace(300), 60)
        self.assertEqual(heartbeats.default_grace(1500), 300)
        self.assertEqual(heartbeats.default_grace(86400), 3600)

    def test_a_fast_heartbeat_needs_three_good_pings_and_a_slow_one_needs_one(self):
        self.assertEqual(heartbeats.pings_needed(60), 3)
        self.assertEqual(heartbeats.pings_needed(3599), 3)
        self.assertEqual(heartbeats.pings_needed(3600), 1)
        self.assertEqual(heartbeats.pings_needed(86400), 1)

    def test_state_is_ok_then_late_then_missed(self):
        hb = self.hb()  # due at +300 s, missed after +360 s
        self.assertEqual(heartbeats.state_of(hb, T0 + timedelta(seconds=299)), "ok")
        self.assertEqual(heartbeats.state_of(hb, T0 + timedelta(seconds=301)), "late")
        self.assertEqual(heartbeats.state_of(hb, T0 + timedelta(seconds=361)), "missed")

    def test_pinging_again_with_the_issue_still_open_is_recovering_not_missed(self):
        fresh = T0 + timedelta(seconds=1)
        self.assertEqual(heartbeats.state_of(self.hb(missed_open=True), fresh), "recovering", "its pings are arriving")
        self.assertEqual(heartbeats.state_of(self.hb(failed_open=True), fresh), "recovering", "failed once, ok since")
        self.assertEqual(heartbeats.state_of(self.hb(failed_open=True, last_status="fail"), fresh), "failed")

    def test_silence_beats_everything_else(self):
        silent = T0 + timedelta(seconds=361)
        self.assertEqual(heartbeats.state_of(self.hb(missed_open=True), silent), "missed", "went quiet again mid-recovery")
        self.assertEqual(heartbeats.state_of(self.hb(failed_open=True, last_status="fail"), silent), "missed")

    def test_the_event_is_fatal_only_for_a_production_app(self):
        def level(kind, env):
            hb = Heartbeat(name="x", kind=kind, interval_s=60, grace_s=60, last_ping_at=T0)
            return heartbeats.make_event(Site(env=env, name="s"), hb, heartbeats.MISSED).level

        self.assertEqual(level("app", "production"), "fatal")
        self.assertEqual(level("job", "production"), "error")
        self.assertEqual(level("app", "staging"), "error", "nothing is down for a client")

    def test_the_name_is_in_a_frame_so_two_heartbeats_are_two_issues(self):
        site = Site(id=1, env="production", name="s")
        a = Heartbeat(name="backup", kind="job", interval_s=60, grace_s=60, last_ping_at=T0)
        b = Heartbeat(name="cleanup", kind="job", interval_s=60, grace_s=60, last_ping_at=T0)
        ea, eb = (heartbeats.make_event(site, h, heartbeats.MISSED) for h in (a, b))
        self.assertEqual(ea.message, eb.message, "fixed text: the same heartbeat silent twice is one issue")
        self.assertNotEqual(heartbeats._fingerprint(site, ea), heartbeats._fingerprint(site, eb))


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
        cls.name = f"nj_hb_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE clients, sites, issues, events, notifications, issue_actions, heartbeats RESTART IDENTITY CASCADE")
            )
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            prod = Site(client_id=client.id, name="acme-shop", env="production", ingest_key=KEY, ingest_secret="s")
            stage = Site(client_id=client.id, name="acme-shop-staging", env="staging", ingest_key="stage-key", ingest_secret="s")
            session.add_all([prod, stage])
            await session.commit()
            self.prod_id, self.stage_id = prod.id, stage.id
        # a known console token whatever the developer's .env says, sent by default: ingest ignores it
        p = patch.object(settings, "console_token", "test-console-token-0123456789")
        p.start()
        self.addCleanup(p.stop)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-console-token-0123456789"},
        )

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    # --- helpers -------------------------------------------------------------------------------------------

    async def ping(self, at, name="nightly", site_id=None, **body):
        async with self.maker() as session:
            site = await session.get(Site, site_id or self.prod_id)
            await heartbeats.record_ping(session, site, name, HeartbeatIn(**body), now=at)

    async def sweep(self, at):
        async with self.maker() as session:
            return await heartbeats.reconcile(session, now=at)

    async def issues(self):
        async with self.maker() as session:
            return list((await session.scalars(select(Issue).order_by(Issue.id))).all())

    async def alerts(self):
        async with self.maker() as session:
            return list((await session.scalars(select(Notification).order_by(Notification.id))).all())

    async def row(self, name="nightly"):
        async with self.maker() as session:
            return await session.scalar(select(Heartbeat).where(Heartbeat.name == name))

    # --- registering ---------------------------------------------------------------------------------------

    async def test_a_bare_ping_registers_a_job_with_the_defaults_and_raises_nothing(self):
        await self.ping(T0)
        hb = await self.row()
        self.assertEqual((hb.kind, hb.interval_s, hb.grace_s), ("job", 300, 60))
        self.assertEqual(await self.issues(), [])
        self.assertEqual(await self.sweep(T0 + timedelta(seconds=10)), 0)

    async def test_the_latest_ping_decides_the_schedule(self):
        await self.ping(T0, interval_s=60, kind="app")
        await self.ping(T0 + timedelta(seconds=30), interval_s=86400)
        hb = await self.row()
        self.assertEqual((hb.kind, hb.interval_s, hb.grace_s), ("app", 86400, 3600), "kind kept, grace re-derived")
        await self.ping(T0 + timedelta(seconds=60), grace_s=7)
        self.assertEqual((await self.row()).grace_s, 7, "an explicit grace is kept")

    # --- silence -------------------------------------------------------------------------------------------

    async def test_silence_inside_the_grace_opens_nothing(self):
        await self.ping(T0)  # missed after 360 s
        self.assertEqual(await self.sweep(T0 + timedelta(seconds=359)), 0)
        self.assertEqual(await self.issues(), [])

    async def test_silence_opens_one_issue_and_one_alert_however_long_it_lasts(self):
        await self.ping(T0)
        for minutes in range(7, 40):
            await self.sweep(T0 + timedelta(minutes=minutes))
        [issue] = await self.issues()
        self.assertEqual((issue.status, issue.severity, issue.event_count), ("open", "P2", 1), "edge-triggered, not per round")
        self.assertIn("HeartbeatMissed", issue.title)
        self.assertEqual(len(await self.alerts()), 1)

    async def test_a_production_app_that_goes_silent_is_a_p1(self):
        await self.ping(T0, kind="app")
        await self.sweep(T0 + timedelta(minutes=7))
        [issue] = await self.issues()
        self.assertEqual(issue.severity, "P1")

    async def test_a_staging_app_that_goes_silent_is_only_a_p2(self):
        await self.ping(T0, kind="app", site_id=self.stage_id)
        await self.sweep(T0 + timedelta(minutes=7))
        [issue] = await self.issues()
        self.assertEqual(issue.severity, "P2")

    async def test_two_heartbeats_on_one_site_are_two_issues(self):
        await self.ping(T0, name="backup")
        await self.ping(T0, name="cleanup")
        self.assertEqual(await self.sweep(T0 + timedelta(minutes=7)), 2)
        self.assertEqual(len(await self.issues()), 2)

    async def test_a_muted_issue_stays_quiet(self):
        await self.ping(T0)
        await self.sweep(T0 + timedelta(minutes=7))
        async with self.maker() as session:
            issue = await session.scalar(select(Issue))
            issue.status = "muted"
            await session.commit()
        await self.ping(T0 + timedelta(minutes=8))
        await self.sweep(T0 + timedelta(hours=2))  # silent again
        self.assertEqual(len(await self.alerts()), 1, "muted never speaks again, including on a new silence")
        self.assertEqual((await self.issues())[0].status, "muted")

    # --- recovery ------------------------------------------------------------------------------------------

    async def test_a_fast_heartbeat_resolves_only_after_three_good_pings(self):
        await self.ping(T0, interval_s=60)  # missed after 120 s
        await self.sweep(T0 + timedelta(minutes=5))
        self.assertEqual((await self.issues())[0].status, "open")
        await self.ping(T0 + timedelta(minutes=6))
        await self.ping(T0 + timedelta(minutes=7))
        self.assertEqual((await self.issues())[0].status, "open", "two is not enough: it could still flap")
        await self.ping(T0 + timedelta(minutes=8))
        [issue] = await self.issues()
        self.assertEqual(issue.status, "resolved")
        hb = await self.row()
        self.assertEqual((hb.missed_open, hb.good_streak), (False, 0))
        async with self.maker() as session:
            action = await session.scalar(select(IssueAction).where(IssueAction.action == "resolve"))
        self.assertTrue(action.detail.startswith("auto:"), "logged as automatic")

    async def test_a_nightly_job_resolves_on_its_first_good_ping(self):
        await self.ping(T0, interval_s=86400)  # missed after 25 h
        await self.sweep(T0 + timedelta(hours=26))
        self.assertEqual((await self.issues())[0].status, "open")
        await self.ping(T0 + timedelta(hours=27))
        self.assertEqual((await self.issues())[0].status, "resolved")

    async def test_going_silent_again_mid_recovery_starts_the_count_over(self):
        await self.ping(T0, interval_s=60)
        await self.sweep(T0 + timedelta(minutes=5))
        await self.ping(T0 + timedelta(minutes=6))
        await self.ping(T0 + timedelta(minutes=7))
        await self.sweep(T0 + timedelta(minutes=20))  # silent again
        self.assertEqual((await self.row()).good_streak, 0)
        await self.ping(T0 + timedelta(minutes=21))
        self.assertEqual((await self.issues())[0].status, "open", "two earlier pings must not count")
        self.assertEqual(len(await self.alerts()), 1, "still the same problem: no second alert")

    async def test_silence_after_a_resolve_is_a_regression_and_an_alert(self):
        await self.ping(T0, interval_s=86400)
        await self.sweep(T0 + timedelta(hours=26))
        await self.ping(T0 + timedelta(hours=27))
        await self.sweep(T0 + timedelta(hours=60))
        self.assertEqual((await self.issues())[0].status, "open")
        self.assertEqual([a.change for a in await self.alerts()], ["new", "regression"])

    async def test_a_person_who_resolved_it_first_is_not_overruled(self):
        await self.ping(T0, interval_s=86400)
        await self.sweep(T0 + timedelta(hours=26))
        async with self.maker() as session:
            (await session.scalar(select(Issue))).status = "muted"
            await session.commit()
        await self.ping(T0 + timedelta(hours=27))
        self.assertEqual((await self.issues())[0].status, "muted", "only open issues are touched")
        self.assertFalse((await self.row()).missed_open, "but the heartbeat itself is healthy again")

    # --- an explicit failure -------------------------------------------------------------------------------

    async def test_a_failed_run_opens_one_issue_even_though_the_job_is_alive(self):
        await self.ping(T0, status="fail", message="exit 2")
        await self.ping(T0 + timedelta(minutes=1), status="fail", message="exit 2")
        await self.ping(T0 + timedelta(minutes=2), status="fail", message="exit 2")
        [issue] = await self.issues()
        self.assertIn("HeartbeatFailed", issue.title)
        self.assertEqual(issue.event_count, 1, "edge-triggered")
        self.assertEqual((await self.row()).last_message, "exit 2")

    async def test_good_pings_resolve_a_failure_the_same_way(self):
        await self.ping(T0, status="fail", interval_s=3600)
        await self.ping(T0 + timedelta(hours=1))
        self.assertEqual((await self.issues())[0].status, "resolved")
        self.assertFalse((await self.row()).failed_open)

    async def test_a_failure_while_recovering_does_not_resolve(self):
        await self.ping(T0, interval_s=60)
        await self.sweep(T0 + timedelta(minutes=5))
        await self.ping(T0 + timedelta(minutes=6))
        await self.ping(T0 + timedelta(minutes=7), status="fail")
        await self.ping(T0 + timedelta(minutes=8))
        statuses = {i.title.split(":")[0]: i.status for i in await self.issues()}
        self.assertEqual(statuses, {"HeartbeatMissed": "open", "HeartbeatFailed": "open"})

    # --- the routes ----------------------------------------------------------------------------------------

    async def test_the_route_takes_a_bare_post_and_lists_it_for_the_console(self):
        res = await self.http.post(f"/ingest/{KEY}/heartbeat/nightly")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual((res.json()["name"], res.json()["state"], res.json()["site"]), ("nightly", "ok", "acme-shop"))
        listed = await self.http.get("/api/heartbeats")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([h["name"] for h in listed.json()], ["nightly"])

    async def test_the_route_rejects_a_bad_body_with_422_not_500(self):
        res = await self.http.post(f"/ingest/{KEY}/heartbeat/nightly", json={"interval_s": 1})
        self.assertEqual(res.status_code, 422)
        res = await self.http.post(f"/ingest/{KEY}/heartbeat/nightly", json={"status": "great"})
        self.assertEqual(res.status_code, 422)

    async def test_the_route_rejects_an_odd_name_and_an_unknown_key(self):
        self.assertEqual((await self.http.post(f"/ingest/{KEY}/heartbeat/.hidden")).status_code, 422)
        self.assertEqual((await self.http.post(f"/ingest/{KEY}/heartbeat/" + "x" * 81)).status_code, 422)
        self.assertEqual((await self.http.post("/ingest/nope/heartbeat/nightly")).status_code, 404)

    async def test_a_ping_is_signed_like_every_other_ingest(self):
        with patch.object(settings, "verify_signatures", True):
            self.assertEqual((await self.http.post(f"/ingest/{KEY}/heartbeat/nightly")).status_code, 401)
            self.assertEqual(
                (await self.http.post(f"/ingest/{KEY}/heartbeat/nightly", headers={"X-Hub-Signature": "sha256=00"})).status_code,
                401,
            )

    async def test_the_console_feed_and_delete_need_the_console_token(self):
        bare = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            self.assertEqual((await bare.get("/api/heartbeats")).status_code, 401)
            self.assertEqual((await bare.delete("/api/heartbeats/1")).status_code, 401)
        finally:
            await bare.aclose()

    async def test_a_retired_heartbeat_can_be_deleted_and_stops_being_watched(self):
        await self.ping(T0)
        hb = await self.row()
        self.assertEqual((await self.http.delete(f"/api/heartbeats/{hb.id}")).status_code, 204)
        self.assertEqual((await self.http.delete(f"/api/heartbeats/{hb.id}")).status_code, 404)
        self.assertEqual(await self.sweep(T0 + timedelta(days=1)), 0)


if __name__ == "__main__":
    unittest.main()
