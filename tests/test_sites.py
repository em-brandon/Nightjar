"""Creating and editing sites from the console API, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_sites -v
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


def site(**over):
    return {"name": "kubasu-motors", "client": "Kubasu Motors", "env": "production", **over}


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Sites(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_site_{uuid.uuid4().hex[:10]}"
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
            await conn.execute(text("TRUNCATE clients, sites RESTART IDENTITY CASCADE"))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def test_a_new_site_comes_back_with_keys_that_ingest_accepts(self):
        r = await self.http.post("/api/sites", json=site(url="https://kubasu.example/"))
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual((body["name"], body["client"], body["env"]), ("kubasu-motors", "Kubasu Motors", "production"))
        self.assertTrue(body["ingest_key"] and body["ingest_secret"])
        with patch.object(settings, "verify_signatures", False):
            ev = await self.http.post(
                f"/ingest/{body['ingest_key']}/event", json={"exception_type": "Boom", "message": "x"}
            )
        self.assertEqual(ev.status_code, 200, ev.text)

    async def test_the_secret_is_never_listed_afterwards(self):
        await self.http.post("/api/sites", json=site())
        health = (await self.http.get("/api/sites/health")).text
        self.assertNotIn("ingest_secret", health)

    async def test_a_second_site_for_the_same_client_reuses_the_client(self):
        await self.http.post("/api/sites", json=site())
        r = await self.http.post("/api/sites", json=site(name="kubasu-motors-staging", env="staging"))
        self.assertEqual(r.status_code, 201, r.text)
        async with self.engine.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM clients"))).scalar()
        self.assertEqual(n, 1)

    async def test_a_duplicate_name_is_refused(self):
        await self.http.post("/api/sites", json=site())
        r = await self.http.post("/api/sites", json=site(client="Someone Else"))
        self.assertEqual(r.status_code, 409)

    async def test_staging_and_production_names_must_say_which_they_are(self):
        # promotion pairs <production>-staging with <production> by name
        self.assertEqual((await self.http.post("/api/sites", json=site(env="staging"))).status_code, 422)
        bad = site(name="kubasu-motors-staging")
        self.assertEqual((await self.http.post("/api/sites", json=bad)).status_code, 422)

    async def test_names_are_lowercase_slugs(self):
        for bad in ("Kubasu Motors", "UPPER", "a", "-lead", "has/slash"):
            r = await self.http.post("/api/sites", json=site(name=bad))
            self.assertEqual(r.status_code, 422, bad)

    async def test_only_web_urls_are_accepted(self):
        for bad in ("file:///etc/passwd", "kubasu.co.ke", "ftp://x.example/a", "http://has space.example"):
            r = await self.http.post("/api/sites", json=site(url=bad))
            self.assertEqual(r.status_code, 422, bad)

    async def test_the_media_and_home_hosts_cannot_be_made_here(self):
        for env in ("media", "home"):
            r = await self.http.post("/api/sites", json=site(env=env))
            self.assertEqual(r.status_code, 422, env)

    async def test_setting_the_url_starts_monitoring_and_clearing_it_stops(self):
        created = (await self.http.post("/api/sites", json=site())).json()
        health = {s["name"]: s for s in (await self.http.get("/api/sites/health")).json()}
        self.assertFalse(health["kubasu-motors"]["monitored"])

        r = await self.http.patch(f"/api/sites/{created['id']}", json={"url": "https://kubasu.example/health"})
        self.assertEqual((r.status_code, r.json()["url"]), (200, "https://kubasu.example/health"))
        health = {s["name"]: s for s in (await self.http.get("/api/sites/health")).json()}
        self.assertTrue(health["kubasu-motors"]["monitored"])

        await self.http.patch(f"/api/sites/{created['id']}", json={"url": None})
        health = {s["name"]: s for s in (await self.http.get("/api/sites/health")).json()}
        self.assertFalse(health["kubasu-motors"]["monitored"])

    async def test_allowing_an_origin_turns_browser_reporting_on_and_makes_a_public_key(self):
        created = (await self.http.post("/api/sites", json=site())).json()
        self.assertIsNone(created["public_key"])
        r = await self.http.patch(f"/api/sites/{created['id']}", json={"allowed_origins": ["https://Kubasu.example/"]})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["allowed_origins"], ["https://kubasu.example"])  # lowercased, slash dropped
        self.assertTrue(body["public_key"])
        self.assertNotEqual(body["public_key"], created["ingest_key"], "the public key must not be the ingest key")
        # asking again keeps the same key: it is in pages already deployed
        again = (await self.http.patch(f"/api/sites/{created['id']}", json={"allowed_origins": ["https://kubasu.example", "https://www.kubasu.example"]})).json()
        self.assertEqual(again["public_key"], body["public_key"])

    async def test_emptying_the_origins_revokes_the_key_and_the_next_one_is_new(self):
        sid = (await self.http.post("/api/sites", json=site())).json()["id"]
        first = (await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": ["https://a.example"]})).json()["public_key"]
        off = (await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": []})).json()
        self.assertIsNone(off["public_key"])
        second = (await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": ["https://a.example"]})).json()["public_key"]
        self.assertNotEqual(first, second)

    async def test_an_origin_is_not_a_url_or_a_pattern(self):
        sid = (await self.http.post("/api/sites", json=site())).json()["id"]
        for bad in ("https://*.example.com", "https://a.example/path", "https://a.example?x=1", "a.example",
                    "http://a.example", "ftp://a.example", "https://u:p@a.example"):
            r = await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": [bad]})
            self.assertEqual(r.status_code, 422, bad)

    async def test_plain_http_is_allowed_for_local_development_only(self):
        sid = (await self.http.post("/api/sites", json=site())).json()["id"]
        r = await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": ["http://localhost:5173"]})
        self.assertEqual((r.status_code, r.json()["allowed_origins"]), (200, ["http://localhost:5173"]))

    async def test_a_patch_without_a_url_leaves_the_url_alone(self):
        sid = (await self.http.post("/api/sites", json=site(url="https://kubasu.example/"))).json()["id"]
        r = await self.http.patch(f"/api/sites/{sid}", json={"allowed_origins": ["https://kubasu.example"]})
        self.assertEqual(r.json()["url"], "https://kubasu.example/")

    async def test_an_unknown_site_is_404(self):
        r = await self.http.patch("/api/sites/999", json={"url": "https://x.example/"})
        self.assertEqual(r.status_code, 404)

    async def test_the_health_feed_names_each_sites_client_so_staging_can_be_grouped_with_production(self):
        await self.http.post("/api/sites", json=site())
        await self.http.post("/api/sites", json=site(name="kubasu-motors-staging", env="staging"))
        await self.http.post("/api/sites", json=site(name="mwangaza-school", client="Mwangaza School"))
        health = {s["name"]: s["client"] for s in (await self.http.get("/api/sites/health")).json()}
        self.assertEqual(
            health,
            {"kubasu-motors": "Kubasu Motors", "kubasu-motors-staging": "Kubasu Motors", "mwangaza-school": "Mwangaza School"},
        )

    async def test_the_home_host_site_cannot_be_edited_here(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO clients (name) VALUES ('me')"))
            await conn.execute(
                text("INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) "
                     "VALUES (1, 'thinkcentre', 'home', 'hk', 'hs')")
            )
        r = await self.http.patch("/api/sites/1", json={"url": "https://x.example/"})
        self.assertEqual(r.status_code, 404)

    async def test_the_list_shows_keys_and_never_a_secret(self):
        made = (await self.http.post("/api/sites", json=site())).json()
        r = await self.http.get("/api/sites")
        self.assertEqual(r.status_code, 200)
        row = r.json()[0]
        self.assertEqual((row["name"], row["ingest_key"], row["last_event"]), ("kubasu-motors", made["ingest_key"], None))
        self.assertNotIn("ingest_secret", r.text)
        self.assertNotIn(made["ingest_secret"], r.text)

    async def test_the_list_leaves_out_the_home_host(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO clients (name) VALUES ('me')"))
            await conn.execute(
                text("INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) "
                     "VALUES (1, 'thinkcentre', 'home', 'hk', 'hs')")
            )
        self.assertEqual((await self.http.get("/api/sites")).json(), [])

    async def test_rotating_returns_a_new_secret_once_and_the_old_one_stops_working(self):
        made = (await self.http.post("/api/sites", json=site())).json()
        r = await self.http.post(f"/api/sites/{made['id']}/rotate-secret")
        self.assertEqual(r.status_code, 200, r.text)
        new = r.json()
        self.assertEqual(new["ingest_key"], made["ingest_key"])
        self.assertNotEqual(new["ingest_secret"], made["ingest_secret"])
        self.assertNotIn(new["ingest_secret"], (await self.http.get("/api/sites")).text)
        import hashlib
        import hmac
        import time

        body = b'{"exception_type": "Boom", "message": "x"}'

        async def post(secret):
            ts = str(int(time.time()))
            sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
            with patch.object(settings, "verify_signatures", True):
                return await self.http.post(
                    f"/ingest/{made['ingest_key']}/event",
                    content=body,
                    headers={"X-Hub-Signature": sig, "X-Hub-Timestamp": ts, "Content-Type": "application/json"},
                )

        self.assertEqual((await post(made["ingest_secret"])).status_code, 401)
        self.assertEqual((await post(new["ingest_secret"])).status_code, 200)

    async def test_rotating_an_unknown_site_is_404(self):
        self.assertEqual((await self.http.post("/api/sites/999/rotate-secret")).status_code, 404)

    async def test_a_test_event_goes_through_ingest_as_a_p3_and_does_not_stay_open(self):
        sid = (await self.http.post("/api/sites", json=site())).json()["id"]
        r = await self.http.post(f"/api/sites/{sid}/test-event")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["severity"], r.json()["state_change"]), ("P3", "new"))
        # a test proves the hub side only: it must not make the site look as if its app had reported
        listed = (await self.http.get("/api/sites")).json()[0]
        self.assertIsNone(listed["last_event"])
        open_issues = (await self.http.get("/api/issues?status=open")).json()
        self.assertEqual(open_issues, [])

    async def test_a_second_test_event_stays_quiet_and_does_not_reopen_the_issue(self):
        sid = (await self.http.post("/api/sites", json=site())).json()["id"]
        first = (await self.http.post(f"/api/sites/{sid}/test-event")).json()
        second = (await self.http.post(f"/api/sites/{sid}/test-event")).json()
        self.assertEqual(first["state_change"], "new")
        self.assertIsNone(second["state_change"], "a repeat must not look like a regression")
        self.assertEqual(second["issue_id"], first["issue_id"])
        self.assertEqual(second["event_count"], 2)
        self.assertEqual((await self.http.get("/api/issues?status=open")).json(), [])

    async def test_a_real_report_sets_last_event_and_a_test_does_not_move_it(self):
        made = (await self.http.post("/api/sites", json=site())).json()
        with patch.object(settings, "verify_signatures", False):
            await self.http.post(f"/ingest/{made['ingest_key']}/event", json={"exception_type": "Boom", "message": "x"})
        first = (await self.http.get("/api/sites")).json()[0]["last_event"]
        self.assertIsNotNone(first)
        await self.http.post(f"/api/sites/{made['id']}/test-event")
        self.assertEqual((await self.http.get("/api/sites")).json()[0]["last_event"], first)

    async def test_the_home_host_cannot_be_tested_or_rotated(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO clients (name) VALUES ('me')"))
            await conn.execute(
                text("INSERT INTO sites (client_id, name, env, ingest_key, ingest_secret) "
                     "VALUES (1, 'thinkcentre', 'home', 'hk', 'hs')")
            )
        self.assertEqual((await self.http.post("/api/sites/1/test-event")).status_code, 404)
        self.assertEqual((await self.http.post("/api/sites/1/rotate-secret")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
