"""The home-host report endpoint, the presence tap and the exchange rate, against a real throwaway
database (the rate needs none).

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate, rates  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

KEY = "home-key"
URL = f"/ingest/{KEY}/home"
TOKEN = "a-presence-token-1234567"


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


def iso(delta=timedelta()):
    return (datetime.now(UTC) - delta).isoformat()


def host(**over):
    return {"cpu_temp_c": 52.0, "load_1": 0.4, "cpu_count": 6, "mem_total_mb": 24000, "mem_available_mb": 15000,
            "disk_total_mb": 500000, "disk_free_mb": 300000, "uptime_s": 86400, **over}


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class HomeIngest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_home_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for p in (patch.object(settings, "console_token", ""), patch.object(settings, "presence_token", TOKEN)):
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        await self.sql(
            "TRUNCATE clients, sites, issues, events, issue_actions, home_reports, speed_tests, presence "
            "RESTART IDENTITY CASCADE"
        )
        await self.sql("INSERT INTO clients (name) VALUES ('c')")
        await self.sql(
            "INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) VALUES "
            f"(1, 'thinkcentre', 'home', '{KEY}', 's'), (1, 'shop', 'production', 'other', 's')"
        )
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def sql(self, statement):
        async with self.engine.begin() as conn:
            result = await conn.execute(text(statement))
            return result.all() if result.returns_rows else None

    async def report(self, **over):
        body = {"host": host(), "backup_configured": True, "backup_ok_at": iso(timedelta(hours=6)), **over}
        return await self.http.post(URL, json=body)

    async def issues(self, like):
        return await self.sql(f"SELECT status, event_count, severity FROM issues WHERE title LIKE '{like}%'")

    # ---- live view ----

    async def test_a_client_sites_credentials_cannot_post_home_reports(self):
        r = await self.http.post("/ingest/other/home", json={"cpu_temp_c": 40})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(await self.sql("SELECT count(*) FROM home_reports"), [(0,)])

    async def test_a_report_shows_the_host_and_the_backup_age(self):
        self.assertEqual((await self.report()).status_code, 201)
        r = await self.http.get("/api/home")
        self.assertEqual(r.status_code, 200)
        [h] = r.json()
        self.assertEqual((h["site"], h["stale"], h["cpu_temp_c"]), ("thinkcentre", False, 52.0))
        self.assertTrue(5.9 <= h["backup_age_h"] <= 6.1, h["backup_age_h"])

    async def test_the_cabinet_air_temperatures_are_reported_when_the_sensors_exist(self):
        await self.report(host=host(intake_c=29.5, exhaust_c=41.0))
        [h] = (await self.http.get("/api/home")).json()
        self.assertEqual((h["intake_c"], h["exhaust_c"]), (29.5, 41.0))
        await self.report(host=host())  # no sensors: absent, not zero
        [h] = (await self.http.get("/api/home")).json()
        self.assertEqual((h["intake_c"], h["exhaust_c"]), (None, None))

    async def test_network_activity_is_reported(self):
        await self.report(host=host(rx_bps=3_200_000.0, tx_bps=150_000.0))
        [h] = (await self.http.get("/api/home")).json()
        self.assertEqual((h["rx_bps"], h["tx_bps"]), (3_200_000.0, 150_000.0))
        self.assertEqual((await self.report(host=host(rx_bps=-1.0))).status_code, 422)  # never negative

    async def test_a_silent_reporter_is_stale(self):
        await self.report()
        await self.sql("UPDATE home_reports SET reported_at = now() - interval '10 minutes'")
        [h] = (await self.http.get("/api/home")).json()
        self.assertTrue(h["stale"])

    async def test_the_home_host_is_not_a_client_site(self):
        health = (await self.http.get("/api/sites/health")).json()
        self.assertEqual([s["name"] for s in health], ["shop"])

    async def test_bad_payloads_and_keys(self):
        self.assertEqual((await self.http.post(URL, json={"host": {"cpu_temp_c": "hot"}})).status_code, 422)
        self.assertEqual((await self.http.post("/ingest/nope/home", json={})).status_code, 404)

    # ---- internet speed ----

    async def test_a_repeated_speed_test_is_stored_once(self):
        net = {"down_mbps": 94.2, "up_mbps": 21.0, "latency_ms": 18.0, "tested_at": iso(timedelta(minutes=5))}
        for _ in range(3):  # the reporter repeats its last test on every report
            self.assertEqual((await self.report(net=net)).status_code, 201)
        self.assertEqual((await self.sql("SELECT count(*) FROM speed_tests"))[0][0], 1)
        [h] = (await self.http.get("/api/home")).json()
        self.assertEqual((h["net"]["down_mbps"], h["net"]["latency_ms"]), (94.2, 18.0))

    async def test_the_speed_series_is_oldest_first(self):
        for i, mbps in enumerate([50.0, 60.0, 70.0]):
            await self.report(net={"down_mbps": mbps, "tested_at": iso(timedelta(hours=3 - i))})
        [h] = (await self.http.get("/api/home")).json()
        self.assertEqual(h["speed_series"], [50.0, 60.0, 70.0])

    # ---- problems ----

    async def test_a_hot_host_opens_one_p2_issue(self):
        for _ in range(3):
            self.assertEqual((await self.report(host=host(cpu_temp_c=91.0))).status_code, 201)
        rows = await self.issues("HomeHostHot")
        self.assertEqual((len(rows), rows[0][1], rows[0][2]), (1, 1, "P2"))  # edge-triggered, never P1

    async def test_a_full_disk_opens_an_issue(self):
        await self.report(host=host(disk_free_mb=20000))
        self.assertEqual(len(await self.issues("HomeDiskLow")), 1)

    async def test_a_stale_backup_is_a_p3(self):
        await self.report(backup_ok_at=iso(timedelta(hours=50)))
        rows = await self.issues("BackupStale")
        self.assertEqual((len(rows), rows[0][2]), (1, "P3"))

    async def test_a_backup_that_never_ran_is_stale_only_when_it_is_tracked(self):
        await self.report(backup_configured=False, backup_ok_at=None)
        self.assertEqual(await self.issues("BackupStale"), [])
        await self.report(backup_configured=True, backup_ok_at=None)
        self.assertEqual(len(await self.issues("BackupStale")), 1)

    async def test_a_healthy_host_opens_nothing(self):
        await self.report()
        self.assertEqual((await self.sql("SELECT count(*) FROM issues"))[0][0], 0)

    async def test_the_problem_resolves_itself_after_staying_gone(self):
        await self.report(host=host(cpu_temp_c=91.0))
        await self.sql("UPDATE home_reports SET reported_at = now() - interval '12 minutes'")
        for minute in range(11, 0, -1):
            await self.sql(
                "INSERT INTO home_reports (site_id, reported_at, cpu_temp_c, backup_configured) "
                f"VALUES (1, now() - interval '{minute} minutes', 50, false)"
            )
        self.assertEqual((await self.report()).status_code, 201)
        self.assertEqual((await self.issues("HomeHostHot"))[0][0], "resolved")

    # ---- presence ----

    async def test_no_record_means_away(self):
        p = (await self.http.get("/api/presence")).json()
        self.assertEqual((p["state"], p["recorded"]), ("away", False))

    async def test_a_tap_with_a_wrong_or_missing_token_is_refused_and_changes_nothing(self):
        # /presence is public by design (tests/test_auth.py skips it), so its own token check is the only guard
        for body in ({"state": "home", "token": "wrong-token"}, {"state": "home"}):
            r = await self.http.post("/presence", json=body)
            self.assertEqual(r.status_code, 401, body)
        self.assertFalse((await self.http.get("/api/presence")).json()["recorded"])

    async def test_a_tap_sets_home_with_an_expiry(self):
        r = await self.http.post("/presence", json={"state": "home", "token": TOKEN})
        self.assertEqual(r.status_code, 200)
        p = (await self.http.get("/api/presence")).json()
        self.assertEqual((p["state"], p["source"]), ("home", "nfc"))
        # 14 hours by default
        expires = datetime.fromisoformat(p["expires_at"])
        self.assertTrue(timedelta(hours=13, minutes=55) < expires - datetime.now(UTC) < timedelta(hours=14, minutes=1))

    async def test_an_expired_tap_falls_back_to_away(self):
        await self.http.post("/presence", json={"state": "home", "token": TOKEN})
        await self.sql("UPDATE presence SET expires_at = now() - interval '1 minute'")
        p = (await self.http.get("/api/presence")).json()
        self.assertEqual((p["state"], p["source"]), ("away", "expired"))

    async def test_the_latest_tap_wins(self):
        await self.http.post("/presence", json={"state": "home", "token": TOKEN})
        await self.http.post("/presence", json={"state": "away", "token": TOKEN})
        self.assertEqual((await self.http.get("/api/presence")).json()["state"], "away")

    async def test_a_tap_needs_the_token(self):
        self.assertEqual((await self.http.post("/presence", json={"state": "home"})).status_code, 401)
        self.assertEqual((await self.http.post("/presence", json={"state": "home", "token": "wrong"})).status_code, 401)
        with patch.object(settings, "presence_token", ""):
            self.assertEqual((await self.http.post("/presence", json={"state": "home", "token": "x"})).status_code, 503)
        self.assertEqual((await self.sql("SELECT count(*) FROM presence"))[0][0], 0)

    async def test_the_console_can_set_it_by_hand(self):
        r = await self.http.post("/api/presence", json={"state": "home", "ttl_minutes": 60})
        self.assertEqual((r.status_code, r.json()["source"]), (200, "manual"))


class Rates(unittest.IsolatedAsyncioTestCase):
    GOOD = {"result": "success", "time_last_update_unix": 1789948951, "rates": {"KES": 129.571169, "GBP": 0.7474, "CNY": 6.7031}}

    async def asyncSetUp(self):
        rates._cache.update(at=0.0, value=None)
        for p in (patch.object(settings, "console_token", ""), patch.object(settings, "rates_enabled", True)):
            p.start()
            self.addCleanup(p.stop)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()

    async def get(self):
        r = await self.http.get("/api/rates")
        self.assertEqual(r.status_code, 200)  # decoration: never an error status
        return r.json()

    async def test_a_rate_is_served_and_cached(self):
        with patch.object(rates, "_fetch", AsyncMock(return_value=self.GOOD)) as fetch:
            r = await self.get()
            await self.get()
        self.assertEqual((r["available"], r["rate"], r["quote"]), (True, 129.57, "KES"))
        self.assertEqual([(p["code"], p["rate"]) for p in r["pairs"]], [("USD", 129.57), ("GBP", 173.36), ("CNY", 19.33)])
        self.assertEqual(fetch.await_count, 1)

    async def test_a_missing_currency_is_left_out_not_guessed(self):
        data = {"result": "success", "rates": {"KES": 130.0, "GBP": 0.75}}  # no yuan
        with patch.object(rates, "_fetch", AsyncMock(return_value=data)):
            r = await self.get()
        self.assertEqual([p["code"] for p in r["pairs"]], ["USD", "GBP"])

    async def test_a_failure_or_garbage_is_unavailable(self):
        with patch.object(rates, "_fetch", AsyncMock(side_effect=httpx.ConnectError("down"))):
            self.assertFalse((await self.get())["available"])
        with patch.object(rates, "_fetch", AsyncMock(return_value={"result": "error"})):
            self.assertFalse((await self.get())["available"])
        with patch.object(rates, "_fetch", AsyncMock(return_value={"result": "success", "rates": {}})):
            self.assertFalse((await self.get())["available"])

    async def test_an_old_rate_is_marked_stale(self):
        with patch.object(rates, "_fetch", AsyncMock(return_value=self.GOOD)):
            await self.get()
        rates._cache["at"] -= 5 * settings.rates_ttl_min * 60
        with patch.object(rates, "_fetch", AsyncMock(side_effect=httpx.ConnectError("down"))):
            r = await self.get()
        self.assertEqual((r["available"], r["stale"]), (True, True))


if __name__ == "__main__":
    unittest.main()
