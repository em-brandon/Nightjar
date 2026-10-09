"""Browser error reporting, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_browser -v

The route has no secret to check, so most of these prove the LIMITS: what a caller who is
not a well-behaved page can and cannot do.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import browser, migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

ORIGIN = "https://birdbox.example"


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


def err(message="x is undefined", **over):
    return {
        "exception_type": "TypeError",
        "message": message,
        "frames": [{"module": "index-3fa9c1B2.js", "function": "render", "lineno": 10}],
        **over,
    }


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class BrowserIngest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_brw_{uuid.uuid4().hex[:10]}"
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
        browser.reset_limits()
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites RESTART IDENTITY CASCADE"))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        made = (await self.http.post("/api/sites", json={"name": "bird-box", "client": "Bird Box"})).json()
        self.ingest_key = made["ingest_key"]
        patched = (await self.http.patch(f"/api/sites/{made['id']}", json={"allowed_origins": [ORIGIN]})).json()
        self.key = patched["public_key"]
        self.site_id = made["id"]

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def send(self, body, *, key=None, origin=ORIGIN, headers=None, raw=None):
        h = {"Content-Type": "text/plain;charset=UTF-8", **(headers or {})}
        if origin is not None:
            h["Origin"] = origin
        content = raw if raw is not None else json.dumps(body).encode()
        return await self.http.post(f"/ingest/browser/{key or self.key}", content=content, headers=h)

    async def issues(self):
        async with self.engine.connect() as conn:
            rows = await conn.execute(text("SELECT title, severity, level, event_count FROM issues ORDER BY id"))
            return rows.all()

    # ---- the happy path -------------------------------------------------------------------

    async def test_a_page_error_becomes_one_issue(self):
        r = await self.send(err())
        self.assertEqual(r.status_code, 202, r.text)
        rows = await self.issues()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].title.startswith("TypeError"))

    async def test_text_plain_needs_no_preflight_and_the_origin_is_echoed(self):
        r = await self.send(err())
        self.assertEqual(r.headers["access-control-allow-origin"], ORIGIN)

    async def test_the_same_error_a_hundred_times_is_one_issue(self):
        with patch.object(settings, "browser_rate_per_ip_min", 1000), patch.object(settings, "browser_rate_per_key_min", 1000):
            for _ in range(100):
                await self.send(err("Cannot read 'abc123' of undefined"))
        rows = await self.issues()
        self.assertEqual((len(rows), rows[0].event_count), (1, 100))

    async def test_a_rebuilt_bundle_is_the_same_issue(self):
        await self.send(err(frames=[{"module": "index-3fa9c1B2.js", "function": "render"}]))
        await self.send(err(frames=[{"module": "index-Zq81mK0p.js", "function": "render"}]))
        self.assertEqual(len(await self.issues()), 1)

    async def test_the_query_string_of_the_page_is_not_stored(self):
        await self.send(err(page="https://birdbox.example/cart?token=SECRET#frag"))
        async with self.engine.connect() as conn:
            payload = (await conn.execute(text("SELECT payload FROM events"))).scalar()
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertEqual(payload["request"]["url"], "https://birdbox.example/cart")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    async def test_what_the_real_reporter_sends_is_accepted_and_grouped(self):
        """The contract between client/browser/nightjar.ts and this route: run the real reporter
        under Node, feed its output to the hub, and check a rebuild is still one issue."""
        script = """
        import { createReporter } from './client/browser/nightjar.ts';
        const out = [];
        const r = createReporter({ hub: 'https://h.example', key: 'K', release: 'v1' },
          { send: (u, b) => out.push(b), now: () => 0, page: () => 'https://birdbox.example/cart?x=1', origin: () => 'https://birdbox.example' });
        for (const hash of ['3fa9c1B2', 'Zq81mK0p']) {
          const e = new TypeError('x is undefined');
          e.stack = `TypeError: x
    at render (https://birdbox.example/assets/index-${hash}.js:10:5)
    at run (https://birdbox.example/assets/index-${hash}.js:30:9)`;
          r.capture(e);
          r.capture(new Error('flush the dedupe window'), 'warning'); // distinct, to prove it is not merged in
        }
        console.log(JSON.stringify(out));
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        done = subprocess.run(["node", "--input-type=module", "-e", script], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        bodies = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(bodies), 2, bodies)
        for body in bodies:
            r = await self.send(None, raw=body.encode())
            self.assertEqual(r.status_code, 202, f"{r.text} for {body}")
        titles = [row.title for row in await self.issues()]
        self.assertEqual(sum(t.startswith("TypeError") for t in titles), 1, titles)

    # ---- who may call ---------------------------------------------------------------------

    async def test_an_unknown_key_is_404(self):
        self.assertEqual((await self.send(err(), key="nope")).status_code, 404)

    async def test_another_website_is_refused(self):
        self.assertEqual((await self.send(err(), origin="https://evil.example")).status_code, 403)
        self.assertEqual(await self.issues(), [])

    async def test_a_call_with_no_origin_is_refused(self):
        # a browser always sends one; its absence means curl or a script
        self.assertEqual((await self.send(err(), origin=None)).status_code, 403)

    async def test_the_public_key_opens_nothing_else(self):
        for path in (f"/ingest/{self.key}/event", f"/ingest/{self.key}/deploy", f"/ingest/{self.key}/stack"):
            r = await self.http.post(path, json=err(), headers={"Origin": ORIGIN})
            self.assertEqual(r.status_code, 404, path)
        self.assertEqual(len(await self.issues()), 0)

    async def test_the_ingest_key_is_not_the_public_key(self):
        self.assertNotEqual(self.key, self.ingest_key)
        self.assertEqual((await self.send(err(), key=self.ingest_key)).status_code, 404)

    async def test_removing_the_origins_revokes_the_key(self):
        await self.http.patch(f"/api/sites/{self.site_id}", json={"allowed_origins": []})
        self.assertEqual((await self.send(err())).status_code, 404)

    async def test_preflight_is_answered_only_for_an_allowed_origin(self):
        ok = await self.http.options(f"/ingest/browser/{self.key}", headers={"Origin": ORIGIN})
        self.assertEqual((ok.status_code, ok.headers["access-control-allow-origin"]), (204, ORIGIN))
        bad = await self.http.options(f"/ingest/browser/{self.key}", headers={"Origin": "https://evil.example"})
        self.assertEqual(bad.status_code, 403)

    # ---- what a caller may say ------------------------------------------------------------

    async def test_a_page_cannot_ask_for_p1(self):
        await self.send(err("a", level="fatal"))
        await self.send(err("b", level="warning"))
        await self.send(err("c", level="made-up"))
        sev = {r.title.split(": ")[1]: (r.severity, r.level) for r in await self.issues()}
        self.assertEqual(sev, {"a": ("P2", "error"), "b": ("P3", "warning"), "c": ("P2", "error")})

    async def test_an_oversized_report_is_refused(self):
        r = await self.send(err("x" * 40_000))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(await self.issues(), [])

    async def test_bad_json_is_422_not_500(self):
        for raw in (b"not json", b"{}", b'{"exception_type": ""}', b'{"exception_type": "E", "frames": "no"}'):
            r = await self.send(None, raw=raw)
            self.assertEqual(r.status_code, 422, raw)

    async def test_free_form_extra_fields_are_dropped(self):
        await self.send(err(extra={"cookie": "SESSION"}, request={"headers": "x"}))
        async with self.engine.connect() as conn:
            payload = json.dumps((await conn.execute(text("SELECT payload FROM events"))).scalar())
        self.assertNotIn("SESSION", payload)

    # ---- the brakes -----------------------------------------------------------------------

    async def test_one_visitor_is_rate_limited(self):
        with patch.object(settings, "browser_rate_per_ip_min", 3):
            codes = [(await self.send(err("same"), headers={"X-Forwarded-For": "203.0.113.9"})).status_code for _ in range(5)]
            other = await self.send(err("same"), headers={"X-Forwarded-For": "203.0.113.10"})
        self.assertEqual(codes, [202, 202, 202, 429, 429])
        self.assertEqual(other.status_code, 202, "another visitor is not punished for the first")

    async def test_the_key_has_its_own_ceiling_even_if_visitors_are_faked(self):
        with patch.object(settings, "browser_rate_per_key_min", 4), patch.object(settings, "browser_rate_per_ip_min", 1000):
            codes = [(await self.send(err("same"), headers={"X-Forwarded-For": f"203.0.113.{i}"})).status_code for i in range(7)]
        self.assertEqual(codes, [202, 202, 202, 202, 429, 429, 429])

    async def test_a_flood_of_different_errors_opens_only_a_few_issues(self):
        with patch.object(settings, "browser_new_issues_per_hour", 3), patch.object(settings, "browser_rate_per_ip_min", 1000), patch.object(settings, "browser_rate_per_key_min", 1000):
            for i in range(40):
                r = await self.send(err(f"unique failure kind {chr(97 + i % 26)}{chr(97 + i // 26)}"))
                self.assertEqual(r.status_code, 202)  # dropped quietly: the caller learns nothing
        self.assertEqual(len(await self.issues()), 3)

    async def test_an_error_already_known_is_still_counted_when_the_cap_is_reached(self):
        with patch.object(settings, "browser_new_issues_per_hour", 1), patch.object(settings, "browser_rate_per_ip_min", 1000):
            await self.send(err("the real bug"))
            await self.send(err("a new thing"))  # over the cap: dropped
            await self.send(err("the real bug"))  # known: counted
        rows = await self.issues()
        self.assertEqual([(r.title.split(": ")[1], r.event_count) for r in rows], [("the real bug", 2)])


if __name__ == "__main__":
    unittest.main()
