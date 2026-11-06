"""Console sign-in with an account: password, authenticator code, server-side sessions.

    python -m unittest tests.test_account_login -v

Runs against its own migrated throwaway database, so it does not depend on what the dev database holds. Skipped, by
name, when Postgres is not reachable.
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import auth, credentials, migrate  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402
from hub.models import AuditLog, ConsoleSession, Operator  # noqa: E402
from tests.test_auth import _admin, _reachable  # noqa: E402

TOKEN = "correct-horse-battery-staple-1234"
MASTER = "m" * 40
USER = "brandon"
PASSWORD = "a long passphrase for tests"
FIRST_STEP = 60_000_000


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class AccountLogin(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_acct_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)
        # hashing costs ~100 ms; do it once
        cls.password_hash = credentials.hash_password(PASSWORD)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for p in (
            patch.object(settings, "console_token", TOKEN),
            patch.object(settings, "secret_master_key", MASTER),
            patch.object(settings, "operator_login", True),
            patch("hub.auth.asyncio.sleep", new=AsyncMock()),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.step = FIRST_STEP
        step_patch = patch.object(credentials, "totp_step", side_effect=lambda now=None: self.step)
        step_patch.start()
        self.addCleanup(step_patch.stop)
        for d in (auth._failures, auth._account_failures, auth._ip_failures):
            d.clear()
            self.addCleanup(d.clear)

        self.engine = create_async_engine(self.url, poolclass=pool.NullPool)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE operators, console_sessions, audit_log RESTART IDENTITY CASCADE"))
        self.codes = credentials.new_recovery_codes(3)
        async with self.maker() as db:
            op = Operator(
                username=USER,
                password_hash=self.password_hash,
                totp_version=1,
                totp_last_step=0,
                recovery_hashes=[credentials.hash_recovery(c) for c in self.codes],
            )
            db.add(op)
            await db.commit()
            self.op_id = op.id
        self.secret = credentials.totp_secret(MASTER, self.op_id, 1)

    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_session, None)
        await self.engine.dispose()

    # helpers ---------------------------------------------------------------------------------------------------
    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def code(self, advance=True) -> str:
        """A currently valid authenticator code. Each sign-in spends its step, so the clock moves on after."""
        code = credentials._code(self.secret, self.step)
        if advance:
            self.step += 1
        return code

    async def login(self, c, **over):
        body = {"username": USER, "password": PASSWORD, "code": self.code()}
        body.update(over)
        return await c.post("/api/auth/login", json=body)

    async def sessions(self):
        async with self.maker() as db:
            return list(await db.scalars(select(ConsoleSession).order_by(ConsoleSession.id)))

    async def audit(self):
        async with self.maker() as db:
            return [(a.action, a.detail, a.actor) for a in await db.scalars(select(AuditLog).order_by(AuditLog.id))]


class SignIn(AccountLogin):
    async def test_the_right_password_and_code_signs_in_with_a_hardened_cookie(self):
        async with self.client() as c:
            self.assertEqual((await c.get("/api/auth/status")).json(), {"required": True, "authenticated": False, "account": True})
            r = await self.login(c)
            self.assertEqual(r.status_code, 200, r.text)
            cookie = r.headers["set-cookie"]
            self.assertIn(f"{auth.SESSION_COOKIE}=", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            self.assertEqual((await c.get("/api/auth/status")).json()["authenticated"], True)
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 200)

    async def test_the_database_holds_only_a_hash_of_the_session_never_the_cookie_value(self):
        async with self.client() as c:
            r = await self.login(c)
            value = r.cookies[auth.SESSION_COOKIE]
            (row,) = await self.sessions()
            self.assertEqual(row.token_hash, credentials.hash_token(value))
            self.assertNotEqual(row.token_hash, value)

    async def test_the_shared_console_token_cannot_sign_in_any_more(self):
        async with self.client() as c:
            r = await c.post("/api/auth/login", json={"token": TOKEN})
            self.assertEqual(r.status_code, 401)
            self.assertNotIn("set-cookie", r.headers)

    async def test_a_token_mode_cookie_is_worth_nothing_in_account_mode(self):
        async with self.client() as c:
            c.cookies.set(auth.COOKIE, auth._session_value())
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 401)

    async def test_wrong_username_password_and_code_all_look_the_same(self):
        async with self.client() as c:
            answers = set()
            for over in ({"username": "nobody"}, {"password": "wrong passphrase!!"}, {"code": "000000"}, {"code": ""}):
                r = await self.login(c, **over)
                self.assertEqual(r.status_code, 401, over)
                self.assertNotIn("set-cookie", r.headers)
                answers.add(r.text)
            self.assertEqual(len(answers), 1, f"the answer must not say which part was wrong: {answers}")
            self.assertEqual(await self.sessions(), [])

    async def test_a_username_is_not_case_sensitive(self):
        async with self.client() as c:
            self.assertEqual((await self.login(c, username=" Brandon ")).status_code, 200)

    async def test_a_code_cannot_be_replayed(self):
        async with self.client() as c:
            spent = self.code(advance=False)
            self.assertEqual((await self.login(c, code=spent)).status_code, 200)
            self.step += 0  # same moment: the very same code is offered again
            self.assertEqual((await self.login(c, code=spent)).status_code, 401)

    async def test_a_recovery_code_works_once_and_is_then_gone(self):
        async with self.client() as c:
            self.assertEqual((await self.login(c, code=self.codes[0])).status_code, 200)
            self.assertEqual((await self.login(c, code=self.codes[0])).status_code, 401)
            self.assertEqual((await self.login(c, code=self.codes[1].upper())).status_code, 200)
            async with self.maker() as db:
                left = (await db.get(Operator, self.op_id)).recovery_hashes
            self.assertEqual(len(left), 1)
            self.assertIn(("recovery_code_used", "2 left", USER), await self.audit())

    async def test_no_master_key_means_nobody_signs_in_rather_than_a_guessable_code(self):
        async with self.client() as c:
            with patch.object(settings, "secret_master_key", ""):
                self.assertEqual((await self.login(c)).status_code, 401)


class Sessions(AccountLogin):
    async def test_logout_ends_the_session_on_the_server_so_a_copied_cookie_dies_too(self):
        async with self.client() as c, self.client() as thief:
            r = await self.login(c)
            thief.cookies.set(auth.SESSION_COOKIE, r.cookies[auth.SESSION_COOKIE])
            self.assertEqual((await thief.get("/api/errors/rising")).status_code, 200)
            await c.post("/api/auth/logout")
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 401)
            self.assertEqual((await thief.get("/api/errors/rising")).status_code, 401, "revoked on the server, not just cleared in one browser")

    async def test_a_session_past_its_expiry_is_refused(self):
        async with self.client() as c:
            await self.login(c)
            async with self.maker() as db:
                row = (await db.scalars(select(ConsoleSession))).one()
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 401)

    async def test_a_session_unused_for_too_long_is_refused(self):
        async with self.client() as c:
            await self.login(c)
            async with self.maker() as db:
                row = (await db.scalars(select(ConsoleSession))).one()
                row.last_seen_at = datetime.now(timezone.utc) - timedelta(days=settings.session_idle_days, minutes=1)
                await db.commit()
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 401)

    async def test_use_keeps_a_session_alive_but_writes_at_most_once_a_minute(self):
        async with self.client() as c:
            await self.login(c)
            (row,) = await self.sessions()
            first = row.last_seen_at
            await c.get("/api/errors/rising")
            (row,) = await self.sessions()
            self.assertEqual(row.last_seen_at, first, "a poll inside the minute is not a write")
            async with self.maker() as db:
                r = (await db.scalars(select(ConsoleSession))).one()
                r.last_seen_at = first - timedelta(minutes=5)
                await db.commit()
            await c.get("/api/errors/rising")
            (row,) = await self.sessions()
            self.assertGreater(row.last_seen_at, first - timedelta(minutes=5))

    async def test_you_can_list_your_sessions_and_end_another_one(self):
        async with self.client() as laptop, self.client() as phone:
            await self.login(laptop)
            await self.login(phone)
            listed = (await laptop.get("/api/auth/sessions")).json()
            self.assertEqual(len(listed), 2)
            other = next(s for s in listed if not s["current"])
            self.assertEqual((await laptop.delete(f"/api/auth/sessions/{other['id']}")).status_code, 200)
            self.assertEqual((await phone.get("/api/errors/rising")).status_code, 401)
            self.assertEqual((await laptop.get("/api/errors/rising")).status_code, 200)

    async def test_sign_out_everywhere_else_keeps_this_one(self):
        async with self.client() as laptop, self.client() as phone:
            await self.login(laptop)
            await self.login(phone)
            r = await laptop.post("/api/auth/sessions/revoke-others")
            self.assertEqual(r.json(), {"revoked": 1})
            self.assertEqual((await phone.get("/api/errors/rising")).status_code, 401)
            self.assertEqual((await laptop.get("/api/errors/rising")).status_code, 200)

    async def test_session_routes_need_a_real_session(self):
        async with self.client() as c:
            for method, path in (("GET", "/api/auth/sessions"), ("DELETE", "/api/auth/sessions/1"), ("POST", "/api/auth/sessions/revoke-others")):
                self.assertEqual((await c.request(method, path)).status_code, 401, path)
                bearer = await c.request(method, path, headers={"Authorization": f"Bearer {TOKEN}"})
                self.assertEqual(bearer.status_code, 403, f"{path}: a bearer token has no account behind it")


class Bearer(AccountLogin):
    async def test_the_bearer_token_still_works_for_scripts_and_the_wall(self):
        async with self.client() as c:
            r = await c.get("/api/errors/rising", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual((await c.get("/api/errors/rising", headers={"Authorization": "Bearer nope"})).status_code, 401)


class Throttle(AccountLogin):
    async def test_guessing_locks_the_account_even_against_the_right_credentials(self):
        async with self.client() as c:
            for _ in range(auth.ACCOUNT_FAIL_LIMIT):
                self.assertEqual((await self.login(c, password="wrong passphrase!!")).status_code, 401)
            self.assertEqual((await self.login(c)).status_code, 429, "a guesser gains nothing from the lock lifting on a right answer")
            r = await c.get("/api/errors/rising", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(r.status_code, 200, "the lock never reaches the bearer token")

    async def test_one_address_cannot_try_many_usernames(self):
        async with self.client() as c:
            for i in range(auth.IP_FAIL_LIMIT):
                self.assertEqual((await self.login(c, username=f"user{i}")).status_code, 401)
            self.assertEqual((await self.login(c)).status_code, 429)

    async def test_a_good_sign_in_clears_the_count(self):
        async with self.client() as c:
            for _ in range(auth.ACCOUNT_FAIL_LIMIT - 1):
                await self.login(c, password="wrong passphrase!!")
            self.assertEqual((await self.login(c)).status_code, 200)
            for _ in range(auth.ACCOUNT_FAIL_LIMIT - 1):
                self.assertEqual((await self.login(c, password="wrong passphrase!!")).status_code, 401)


class Trail(AccountLogin):
    async def test_sign_ins_and_failures_are_recorded_and_hold_no_secret(self):
        async with self.client() as c:
            await self.login(c, password="wrong passphrase!!")
            good = await self.login(c)
            await self.login(c, username="nobody")
            cookie_value = good.cookies[auth.SESSION_COOKIE]
            rows = await self.audit()
            actions = [a for a, _, _ in rows]
            self.assertEqual(actions, ["login_failed", "login"], "an unknown username is not logged: it would let a stranger fill the trail")
            blob = " ".join(f"{a} {d} {u}" for a, d, u in rows)
            for secret in (PASSWORD, "wrong passphrase", cookie_value, TOKEN, MASTER):
                self.assertNotIn(secret, blob)

    async def test_revoking_a_session_is_recorded(self):
        async with self.client() as laptop, self.client() as phone:
            await self.login(laptop)
            await self.login(phone)
            other = next(s for s in (await laptop.get("/api/auth/sessions")).json() if not s["current"])
            await laptop.delete(f"/api/auth/sessions/{other['id']}")
            self.assertIn("session_revoked", [a for a, _, _ in await self.audit()])


class Startup(unittest.TestCase):
    def test_account_mode_without_a_master_key_refuses_to_start(self):
        with patch.object(settings, "console_token", TOKEN), patch.object(settings, "verify_signatures", False), patch.object(
            settings, "operator_login", True
        ), patch.object(settings, "secret_master_key", ""):
            with self.assertRaises(RuntimeError) as cm:
                auth.check_config()
            self.assertIn("SECRET_MASTER_KEY", str(cm.exception))

    def test_production_defaults_to_accounts_and_development_to_the_token(self):
        with patch.object(settings, "console_token", TOKEN), patch.object(settings, "operator_login", None):
            with patch.object(settings, "verify_signatures", True):
                self.assertTrue(auth.operator_mode())
            with patch.object(settings, "verify_signatures", False):
                self.assertFalse(auth.operator_mode())
            with patch.object(settings, "operator_login", False), patch.object(settings, "verify_signatures", True):
                self.assertFalse(auth.operator_mode(), "an explicit setting wins")
        with patch.object(settings, "console_token", ""), patch.object(settings, "operator_login", True):
            self.assertFalse(auth.operator_mode(), "no token means auth is off, whatever else is set")


if __name__ == "__main__":
    unittest.main()
