"""Console authentication tests.

    python -m unittest discover -s tests -v

Most of these never touch the database: the auth dependency runs before any
handler, so a rejected request is rejected before it could read or write a row.
"""

import asyncio
import os
import re
import sys
import unittest
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import auth, migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402

TOKEN = "correct-horse-battery-staple-1234"

# Every method and path the console uses. test_no_console_route_is_left_open
# below walks the real route table too, so a route added later cannot slip by.
PROTECTED = [
    ("GET", "/api/issues"),
    ("GET", "/api/issues/1/log"),
    ("GET", "/api/sites/health"),
    ("GET", "/api/errors/rising"),
    ("GET", "/api/tickets"),
    ("GET", "/api/deploys"),
    ("GET", "/api/stacks"),
    ("POST", "/api/issues/1/actions"),
    ("POST", "/api/tickets"),
    ("POST", "/api/sites"),
    ("PATCH", "/api/sites/1"),
    ("PATCH", "/api/tickets/1"),
    ("DELETE", "/api/stacks?site=a&stack=b"),
]


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


class AuthTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (
            patch.object(settings, "console_token", TOKEN),
            patch.object(settings, "operator_login", False),  # these tests are about the shared token
            patch("hub.auth.asyncio.sleep", new=AsyncMock()),
        ):
            p.start()
            self.addCleanup(p.stop)
        auth._failures.clear()
        self.addCleanup(auth._failures.clear)

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class Rejects(AuthTestCase):
    async def test_every_console_route_rejects_an_anonymous_request(self):
        async with self.client() as c:
            for method, path in PROTECTED:
                with self.subTest(route=f"{method} {path}"):
                    r = await c.request(method, path, json={} if method != "GET" else None)
                    self.assertEqual(r.status_code, 401, r.text)

    async def test_no_route_is_left_open_unless_it_is_explicitly_public(self):
        """Ask the app for its real routes and hit each one anonymously, so a route
        added later cannot slip through. Behaviour, not internals: FastAPI wraps
        included routers, so inspecting app.routes would silently find nothing."""
        schema = app.openapi()
        checked, open_routes = [], []
        async with self.client() as c:
            for path, methods in schema["paths"].items():
                if path in ("/healthz", "/presence") or path.startswith("/ingest/") or path.startswith("/api/auth/"):
                    # public by design: probes, per-site-key ingest, login itself, and the NFC presence
                    # tap, which is authenticated by a token in its body (tests/test_home_ingest.py)
                    continue
                url = path.replace("{ingest_key}", "x").replace("{issue_id}", "1").replace("{ticket_id}", "1").replace("{site_id}", "1").replace("{notification_id}", "1")
                for method in methods:
                    if method not in ("get", "post", "patch", "put", "delete"):
                        continue
                    r = await c.request(method.upper(), url, json={} if method != "get" else None)
                    checked.append(f"{method.upper()} {path}")
                    if r.status_code != 401:
                        open_routes.append(f"{method.upper()} {path} -> {r.status_code}")
        # a guard against this test quietly checking nothing
        self.assertGreaterEqual(len(checked), 10, f"only found {checked}")
        self.assertEqual(open_routes, [], "routes reachable without the console token")

    async def test_wrong_bearer_token_is_rejected(self):
        async with self.client() as c:
            r = await c.get("/api/errors/rising", headers={"Authorization": "Bearer nope"})
            self.assertEqual(r.status_code, 401)

    async def test_a_forged_cookie_is_rejected(self):
        async with self.client() as c:
            c.cookies.set(auth.COOKIE, "0" * 64)
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 401)

    async def test_the_token_itself_is_not_accepted_as_the_cookie_value(self):
        # the cookie holds a derived value, never the secret
        async with self.client() as c:
            c.cookies.set(auth.COOKIE, TOKEN)
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 401)


class Accepts(AuthTestCase):
    async def test_the_right_bearer_token_authenticates(self):
        async with self.client() as c:
            r = await c.get("/api/auth/status", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(r.json(), {"required": True, "authenticated": True, "role": "owner", "username": "console", "account": False})

    async def test_login_sets_a_hardened_cookie_that_then_authenticates(self):
        async with self.client() as c:
            self.assertEqual((await c.get("/api/auth/status")).json(), {"required": True, "authenticated": False, "role": None, "username": None, "account": False})

            r = await c.post("/api/auth/login", json={"token": TOKEN})
            self.assertEqual(r.status_code, 200)
            cookie = r.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            self.assertNotIn(TOKEN, cookie, "the secret must never be in the cookie")

            self.assertEqual((await c.get("/api/auth/status")).json()["authenticated"], True)

    async def test_logout_ends_the_session(self):
        async with self.client() as c:
            await c.post("/api/auth/login", json={"token": TOKEN})
            await c.post("/api/auth/logout")
            self.assertEqual((await c.get("/api/auth/status")).json()["authenticated"], False)

    async def test_cookie_is_secure_behind_https_and_not_on_plain_http(self):
        async with self.client() as c:
            plain = await c.post("/api/auth/login", json={"token": TOKEN})
            self.assertNotIn("Secure", plain.headers["set-cookie"])
            behind_caddy = await c.post("/api/auth/login", json={"token": TOKEN}, headers={"X-Forwarded-Proto": "https"})
            self.assertIn("Secure", behind_caddy.headers["set-cookie"])

    async def test_changing_the_token_signs_everyone_out(self):
        async with self.client() as c:
            await c.post("/api/auth/login", json={"token": TOKEN})
            self.assertTrue((await c.get("/api/auth/status")).json()["authenticated"])
            with patch.object(settings, "console_token", "a-completely-different-token-99"):
                self.assertFalse((await c.get("/api/auth/status")).json()["authenticated"])

    async def test_health_stays_reachable_without_the_console_token(self):
        async with self.client() as c:
            self.assertEqual((await c.get("/healthz")).status_code, 200)


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class IngestIsOpen(AuthTestCase):
    """Ingest authenticates by per-site key. If the console token gated it, every
    client app would stop reporting the moment auth was turned on. An unknown key
    must therefore get 404 ("unknown site"), never 401. Runs against its own
    migrated throwaway database, so it does not depend on what the dev or CI
    database happens to contain."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_auth_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    def setUp(self):
        super().setUp()
        self.engine = create_async_engine(self.url, poolclass=pool.NullPool)
        maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with maker() as session:
                yield session

        app.dependency_overrides[get_session] = override

    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_session, None)
        await self.engine.dispose()

    async def test_ingest_is_not_asking_for_the_console_token(self):
        paths = [p for p in app.openapi()["paths"] if p.startswith("/ingest/")]
        self.assertGreaterEqual(len(paths), 3, paths)  # event, deploy, stack
        async with self.client() as c:
            for path in paths:
                # fill every path parameter ({ingest_key}, {public_key}, ...) with an unknown key
                url = re.sub(r"\{[^}]+\}", "no-such-key", path)
                r = await c.post(url, json={})
                self.assertEqual(r.status_code, 404, f"{path}: {r.status_code} {r.text}")


class Login(AuthTestCase):
    async def test_wrong_token_is_401_and_sets_no_cookie(self):
        async with self.client() as c:
            r = await c.post("/api/auth/login", json={"token": "wrong"})
            self.assertEqual(r.status_code, 401)
            self.assertNotIn("set-cookie", r.headers)

    async def test_guessing_is_rate_limited_even_for_the_right_token(self):
        async with self.client() as c:
            for _ in range(auth.FAIL_LIMIT):
                self.assertEqual((await c.post("/api/auth/login", json={"token": "wrong"})).status_code, 401)
            # locked out: the correct token is refused too, so a guesser gains nothing
            self.assertEqual((await c.post("/api/auth/login", json={"token": TOKEN})).status_code, 429)

    async def test_oversized_token_is_rejected_by_validation(self):
        async with self.client() as c:
            self.assertEqual((await c.post("/api/auth/login", json={"token": "x" * 500})).status_code, 422)

    async def test_auth_off_means_open_and_login_is_a_noop(self):
        with patch.object(settings, "console_token", ""):
            async with self.client() as c:
                self.assertEqual((await c.get("/api/auth/status")).json(), {"required": False, "authenticated": True, "role": "owner", "username": "console", "account": False})
                self.assertEqual((await c.post("/api/auth/login", json={"token": "anything"})).status_code, 200)


class StartupGuard(unittest.TestCase):
    def test_production_without_a_token_refuses_to_start(self):
        with patch.object(settings, "verify_signatures", True), patch.object(settings, "console_token", ""):
            with self.assertRaises(RuntimeError) as cm:
                auth.check_config()
            self.assertIn("CONSOLE_TOKEN", str(cm.exception))

    def test_production_with_a_token_and_a_master_key_starts(self):
        with patch.object(settings, "verify_signatures", True), patch.object(settings, "console_token", TOKEN),                 patch.object(settings, "secret_master_key", "m" * 40):
            auth.check_config()

    def test_production_without_a_master_key_refuses_to_start(self):
        with patch.object(settings, "verify_signatures", True), patch.object(settings, "console_token", TOKEN),                 patch.object(settings, "secret_master_key", ""):
            with self.assertRaises(RuntimeError) as cm:
                auth.check_config()
            self.assertIn("SECRET_MASTER_KEY", str(cm.exception))
            self.assertIn("/run/secrets/secret_master_key", str(cm.exception))

    def test_a_short_master_key_is_refused_anywhere(self):
        for verify in (True, False):
            with self.subTest(verify=verify):
                with patch.object(settings, "verify_signatures", verify), patch.object(settings, "console_token", TOKEN),                         patch.object(settings, "secret_master_key", "short"):
                    with self.assertRaises(RuntimeError):
                        auth.check_config()

    def test_local_development_without_a_master_key_is_allowed(self):
        with patch.object(settings, "verify_signatures", False), patch.object(settings, "console_token", TOKEN),                 patch.object(settings, "secret_master_key", ""):
            auth.check_config()

    def test_a_short_token_is_refused_anywhere(self):
        for verify in (True, False):
            with self.subTest(verify=verify):
                with patch.object(settings, "verify_signatures", verify), patch.object(settings, "console_token", "short"):
                    with self.assertRaises(RuntimeError):
                        auth.check_config()

    def test_local_development_without_a_token_is_allowed(self):
        with patch.object(settings, "verify_signatures", False), patch.object(settings, "console_token", ""):
            with self.assertLogs("nightjar.auth", level="WARNING"):
                auth.check_config()


if __name__ == "__main__":
    unittest.main()
