"""The media-report endpoint and its feeds, end to end, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest discover -s tests -v

Every call asserts its HTTP status, for the reason given in test_stack_ingest.py.
"""

import asyncio
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

from hub import migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

KEY = "media-key"
URL = f"/ingest/{KEY}/media"


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


def session_body(**over):
    base = {
        "session_id": "s1",
        "user": "amani",
        "item_id": "ep1",
        "title": "The One with the Pilot",
        "series": "Friends",
        "episode": "S01E01",
        "kind": "Episode",
        "client": "Jellyfin Roku",
        "device": "Living room TV",
        "play_method": "direct_play",
        "paused": False,
        "position_s": 120,
        "runtime_s": 1320,
    }
    return {**base, **over}


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class MediaIngest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_media_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        # auth has its own tests; a developer's local CONSOLE_TOKEN must not fail these
        patcher = patch.object(settings, "console_token", "")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        await self.sql(
            "TRUNCATE clients, sites, issues, events, issue_actions, media_reports, media_plays "
            "RESTART IDENTITY CASCADE"
        )
        await self.sql("INSERT INTO clients (name) VALUES ('c')")
        await self.sql(
            "INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) VALUES "
            f"(1, 'home-media', 'media', '{KEY}', 's'), (1, 'shop', 'production', 'other', 's')"
        )
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def sql(self, statement):
        async with self.engine.begin() as conn:
            result = await conn.execute(text(statement))
            return result.all() if result.returns_rows else None

    async def report(self, up=True, sessions=(), **fields):
        body = {"up": up, "version": "10.10.3", "sessions": list(sessions), **fields}
        return await self.http.post(URL, json=body)

    async def issues(self, title_like):
        return await self.sql(
            f"SELECT status, event_count, severity FROM issues WHERE title LIKE '{title_like}%'"
        )

    # ---- live view ----

    async def test_a_report_shows_who_is_watching_what(self):
        r = await self.report(sessions=[session_body()])
        self.assertEqual(r.status_code, 201)
        feed = await self.http.get("/api/media")
        self.assertEqual(feed.status_code, 200)
        [m] = feed.json()
        self.assertEqual((m["site"], m["up"], m["stale"]), ("home-media", True, False))
        self.assertEqual(m["sessions"][0]["user"], "amani")
        self.assertEqual(m["sessions"][0]["series"], "Friends")

    async def test_a_silent_reporter_shows_as_stale_with_no_sessions(self):
        await self.report(sessions=[session_body()])
        await self.sql("UPDATE media_reports SET reported_at = now() - interval '10 minutes'")
        [m] = (await self.http.get("/api/media")).json()
        self.assertTrue(m["stale"])
        # an old snapshot must not claim someone is still watching
        self.assertEqual(m["sessions"], [])

    async def test_a_client_sites_credentials_cannot_post_media_reports(self):
        # 'shop' is a production site with its own valid key: it may report its own errors and nothing about the media host
        r = await self.http.post("/ingest/other/media", json={"up": True, "version": "1", "sessions": []})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(await self.sql("SELECT count(*) FROM media_reports"), [(0,)])

    async def test_the_media_site_is_not_a_client_site(self):
        health = (await self.http.get("/api/sites/health")).json()
        self.assertEqual([s["name"] for s in health], ["shop"])

    async def test_bad_payloads_and_keys(self):
        self.assertEqual((await self.http.post(URL, json={"sessions": []})).status_code, 422)
        self.assertEqual((await self.http.post("/ingest/nope/media", json={"up": True})).status_code, 404)

    # ---- history ----

    async def test_one_viewing_is_extended_not_duplicated(self):
        await self.report(sessions=[session_body()])
        await self.sql("UPDATE media_plays SET last_seen_at = now() - interval '60 seconds'")
        await self.report(sessions=[session_body(position_s=180)])
        rows = await self.sql("SELECT watched_s, position_s FROM media_plays")
        self.assertEqual(len(rows), 1)
        self.assertTrue(55 <= rows[0][0] <= 65, rows)
        self.assertEqual(rows[0][1], 180)

    async def test_paused_time_is_not_watched_time(self):
        await self.report(sessions=[session_body()])
        await self.sql("UPDATE media_plays SET last_seen_at = now() - interval '60 seconds'")
        await self.report(sessions=[session_body(paused=True)])
        self.assertEqual((await self.sql("SELECT watched_s FROM media_plays"))[0][0], 0)

    async def test_a_reporter_gap_is_not_booked_as_watching(self):
        await self.report(sessions=[session_body()])
        await self.sql("UPDATE media_plays SET last_seen_at = now() - interval '9 minutes'")
        await self.report(sessions=[session_body()])
        self.assertLessEqual((await self.sql("SELECT watched_s FROM media_plays"))[0][0], 120)

    async def test_a_long_gap_starts_a_new_viewing(self):
        await self.report(sessions=[session_body()])
        await self.sql("UPDATE media_plays SET last_seen_at = now() - interval '30 minutes'")
        await self.report(sessions=[session_body()])
        self.assertEqual((await self.sql("SELECT count(*) FROM media_plays"))[0][0], 2)

    async def test_history_feed(self):
        await self.report(sessions=[session_body(), session_body(session_id="s2", user="baraka", item_id="ep9")])
        r = await self.http.get("/api/media/history?days=7")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({p["user"] for p in r.json()}, {"amani", "baraka"})
        await self.sql("UPDATE media_plays SET started_at = now() - interval '30 days'")
        self.assertEqual((await self.http.get("/api/media/history?days=7")).json(), [])
        self.assertEqual(len((await self.http.get("/api/media/history?days=60")).json()), 2)

    # ---- problems ----

    async def test_down_opens_one_p2_issue_and_repeats_do_not_add_more(self):
        for _ in range(3):
            self.assertEqual((await self.report(up=False)).status_code, 201)
        rows = await self.issues("MediaServerDown")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 1)  # edge-triggered: one event, not one per report
        self.assertEqual(rows[0][2], "P2")  # never P1: media may not wake the phone

    async def test_low_disk_opens_an_issue(self):
        await self.report(host_disk_total_mb=1000, host_disk_free_mb=50)
        self.assertEqual(len(await self.issues("MediaDiskLow")), 1)

    async def test_a_healthy_server_opens_nothing(self):
        await self.report(host_disk_total_mb=1000, host_disk_free_mb=600, sessions=[session_body()])
        self.assertEqual((await self.sql("SELECT count(*) FROM issues"))[0][0], 0)

    async def test_the_problem_resolves_itself_after_staying_gone(self):
        await self.report(up=False)
        await self.sql("UPDATE media_reports SET reported_at = now() - interval '12 minutes'")
        for minute in range(11, 0, -1):
            await self.sql(
                "INSERT INTO media_reports (site_id, reported_at, up, sessions) "
                f"VALUES (1, now() - interval '{minute} minutes', true, '[]')"
            )
        self.assertEqual((await self.report(up=True)).status_code, 201)
        self.assertEqual((await self.issues("MediaServerDown"))[0][0], "resolved")
        self.assertTrue((await self.sql("SELECT detail FROM issue_actions"))[0][0].startswith("auto:"))

    async def test_a_brief_recovery_does_not_resolve(self):
        await self.report(up=False)
        await self.report(up=True)
        self.assertEqual((await self.issues("MediaServerDown"))[0][0], "open")


if __name__ == "__main__":
    unittest.main()
