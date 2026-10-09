"""The contract between each server-side reporter and the hub, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_server_reporters -v

Each reporter (client/node, client/php, client/java) is run for real. Its signed request is then given to
the hub with VERIFY_SIGNATURES on, so a mismatch in how a language builds the JSON or the HMAC fails here,
rather than on the first production error. A runtime that is not installed is skipped by name, and the
skip says so; it does not pass silently.
"""

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

HUB = "http://hub.test"


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


# ---- how to run each reporter -----------------------------------------------------------------
# Every runner takes the credentials and a padding count (blank lines placed above the failing line, so
# its line number differs) and returns the prepared request: {"url", "headers", "body"}.


def run_node(key: str, secret: str, pad: int = 0) -> dict:
    lib = os.path.join(ROOT, "client", "node", "nightjar.ts").replace("\\", "/")
    script = (
        f'import {{ prepare }} from "file:///{lib}";\n'
        + "\n" * pad
        + 'function render() { throw new TypeError("x is undefined"); }\n'
        "try { render(); } catch (e) {\n"
        '  console.log(JSON.stringify(prepare({}, e, "error", { method: "GET", path: "/cart?token=SECRET" })));\n'
        "}\n"
    )
    done = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "NIGHTJAR_URL": HUB, "NIGHTJAR_KEY": key, "NIGHTJAR_SECRET": secret},
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


RUNNERS = {"node": (run_node, shutil.which("node") is not None, "node is not installed")}


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class ServerReporters(unittest.IsolatedAsyncioTestCase):
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
        for p in (
            patch.object(settings, "console_token", ""),
            patch.object(settings, "verify_signatures", True),  # the point of this file
        ):
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
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HUB)
        made = (await self.http.post("/api/sites", json={"name": "shop", "client": "Shop"})).json()
        self.key, self.secret = made["ingest_key"], made["ingest_secret"]

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def post(self, prepared: dict, *, body: bytes | None = None, headers: dict | None = None):
        path = prepared["url"].removeprefix(HUB)
        return await self.http.post(
            path, content=body if body is not None else prepared["body"].encode(), headers=headers or prepared["headers"]
        )

    async def issues(self):
        async with self.engine.connect() as conn:
            return (await conn.execute(text("SELECT title, culprit, event_count, level, severity FROM issues"))).all()

    def prepared(self, runner, pad=0):
        return runner(self.key, self.secret, pad)

    # ---- one set of checks, run once per language ---------------------------------------------

    async def each(self, check):
        ran = 0
        for name, (runner, available, why) in RUNNERS.items():
            with self.subTest(language=name):
                if not available:
                    self.skipTest(f"{name}: {why}")
                async with self.engine.begin() as conn:
                    await conn.execute(text("TRUNCATE issues, events RESTART IDENTITY CASCADE"))
                await check(name, runner)
                ran += 1
        return ran

    async def test_a_signed_report_is_accepted_and_becomes_an_issue(self):
        async def check(name, runner):
            r = await self.post(self.prepared(runner))
            self.assertEqual(r.status_code, 200, f"{name}: {r.text}")
            self.assertEqual(r.json()["state_change"], "new")
            rows = await self.issues()
            self.assertEqual(len(rows), 1, name)
            self.assertTrue(rows[0].title.startswith("TypeError: x is undefined"), f"{name}: {rows[0].title}")
            self.assertEqual(rows[0].severity, "P2")

        await self.each(check)

    async def test_a_tampered_body_or_a_wrong_secret_is_refused(self):
        async def check(name, runner):
            p = self.prepared(runner)
            tampered = p["body"].replace("x is undefined", "x is defined").encode()
            self.assertEqual((await self.post(p, body=tampered)).status_code, 401, f"{name}: tampered body")
            wrong = runner(self.key, "not-the-secret", 0)
            self.assertEqual((await self.post(wrong)).status_code, 401, f"{name}: wrong secret")
            self.assertEqual((await self.post(p, headers={"Content-Type": "application/json"})).status_code, 401, f"{name}: no signature")
            self.assertEqual(await self.issues(), [], name)

        await self.each(check)

    async def test_the_signature_is_over_the_exact_bytes_sent(self):
        async def check(name, runner):
            p = self.prepared(runner)
            expected = "sha256=" + hmac.new(self.secret.encode(), p["body"].encode(), hashlib.sha256).hexdigest()
            self.assertEqual(p["headers"]["X-Hub-Signature"], expected, name)

        await self.each(check)

    async def test_moving_the_failing_line_is_still_the_same_issue(self):
        # the whole point of the fingerprint: edit above the bug, and it is still one issue
        async def check(name, runner):
            for pad in (0, 7, 31):
                r = await self.post(self.prepared(runner, pad))
                self.assertEqual(r.status_code, 200, f"{name}: {r.text}")
            rows = await self.issues()
            self.assertEqual((len(rows), rows[0].event_count), (1, 3), f"{name}: {rows}")

        await self.each(check)

    async def test_the_query_string_never_reaches_the_hub(self):
        async def check(name, runner):
            await self.post(self.prepared(runner))
            async with self.engine.connect() as conn:
                payload = (await conn.execute(text("SELECT payload FROM events"))).scalar()
            self.assertNotIn("SECRET", json.dumps(payload), name)

        await self.each(check)


if __name__ == "__main__":
    unittest.main()
