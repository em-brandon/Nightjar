"""The command that creates the console account (hub/operator_cli.py).

    python -m unittest tests.test_operator_cli -v

Runs against a throwaway migrated database with the prompts answered by the test.
"""

import asyncio
import io
import os
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import pool, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import credentials, migrate, operator_cli  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import AuditLog, ConsoleSession, Operator  # noqa: E402
from tests.test_auth import _admin, _reachable  # noqa: E402

MASTER = "m" * 40
PASSWORD = "a long passphrase for tests"


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class OperatorCli(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_cli_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        self.engine = create_async_engine(self.url, poolclass=pool.NullPool)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE operators, console_sessions, audit_log RESTART IDENTITY CASCADE"))
        self.step = 70_000_000
        self.typed: list[str] = []  # what the test "types" at input()
        for p in (
            patch.object(settings, "secret_master_key", MASTER),
            patch.object(operator_cli, "SessionLocal", self.maker),
            patch.object(operator_cli.getpass, "getpass", side_effect=lambda *_: PASSWORD),
            patch.object(operator_cli, "input", side_effect=lambda *_: self.typed.pop(0), create=True),
            patch.object(credentials, "totp_step", side_effect=lambda now=None: self.step),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def run_quiet(self, coro):
        out = io.StringIO()
        with redirect_stdout(out):
            await coro
        return out.getvalue()

    async def operator(self):
        async with self.maker() as db:
            return await db.scalar(select(Operator))

    def right_code(self, op_id: int, version: int = 1) -> str:
        return credentials._code(credentials.totp_secret(MASTER, op_id, version), self.step)

    async def create(self):
        # the account's id is 1 on a freshly truncated table, and the CLI asks for a code that fits it
        self.typed = [self.right_code(1)]
        return await self.run_quiet(operator_cli.create("brandon"))

    async def test_create_saves_a_hashed_password_and_hashed_recovery_codes_after_a_right_code(self):
        out = await self.create()
        op = await self.operator()
        self.assertEqual(op.username, "brandon")
        self.assertTrue(credentials.verify_password(PASSWORD, op.password_hash))
        self.assertEqual(len(op.recovery_hashes), 8)
        self.assertIn(credentials.totp_base32(credentials.totp_secret(MASTER, op.id, 1)), out, "the secret is shown once")
        shown = [line.strip() for line in out.splitlines() if len(line.strip()) == 10 and line.strip().isalnum()]
        self.assertEqual(len(shown), 8)
        for code in shown:
            self.assertIsNotNone(credentials.use_recovery(code, op.recovery_hashes), "what was shown is what was stored")
        for hashed in op.recovery_hashes:
            self.assertNotIn(hashed, out)
        self.assertNotIn(PASSWORD, out)

    async def test_a_wrong_code_is_asked_again_and_nothing_is_saved_until_it_is_right(self):
        self.typed = ["000000", self.right_code(1)]
        await self.run_quiet(operator_cli.create("brandon"))
        self.assertIsNotNone(await self.operator())
        self.assertEqual(self.typed, [], "it asked twice")

    async def test_creating_the_same_account_twice_is_refused(self):
        await self.create()
        with self.assertRaises(SystemExit):
            await self.run_quiet(operator_cli.create("brandon"))

    async def test_it_refuses_without_the_master_key(self):
        with patch.object(settings, "secret_master_key", ""):
            with self.assertRaises(SystemExit):
                await self.run_quiet(operator_cli.create("brandon"))
        self.assertIsNone(await self.operator())

    async def test_changing_the_password_ends_every_session(self):
        await self.create()
        op = await self.operator()
        async with self.maker() as db:
            from datetime import datetime, timedelta, timezone

            now = datetime.now(timezone.utc)
            db.add(ConsoleSession(token_hash="a" * 64, operator_id=op.id, expires_at=now + timedelta(days=1)))
            await db.commit()
        with patch.object(operator_cli.getpass, "getpass", side_effect=lambda *_: "a different long passphrase"):
            await self.run_quiet(operator_cli.passwd("brandon"))
        op = await self.operator()
        self.assertTrue(credentials.verify_password("a different long passphrase", op.password_hash))
        async with self.maker() as db:
            session = await db.scalar(select(ConsoleSession))
        self.assertIsNotNone(session.revoked_at)

    async def test_resetting_the_authenticator_changes_the_secret_the_codes_and_ends_sessions(self):
        await self.create()
        before = await self.operator()
        self.step += 5
        self.typed = [self.right_code(before.id, 2)]
        out = await self.run_quiet(operator_cli.reset_2fa("brandon"))
        after = await self.operator()
        self.assertEqual(after.totp_version, 2)
        self.assertNotEqual(after.recovery_hashes, before.recovery_hashes)
        self.assertIn(credentials.totp_base32(credentials.totp_secret(MASTER, after.id, 2)), out)
        self.assertEqual(after.totp_last_step, 0)

    async def test_the_trail_records_what_was_done_and_never_a_secret(self):
        await self.create()
        with patch.object(operator_cli.getpass, "getpass", side_effect=lambda *_: "a different long passphrase"):
            await self.run_quiet(operator_cli.passwd("brandon"))
        async with self.maker() as db:
            rows = [(a.action, a.detail) for a in await db.scalars(select(AuditLog).order_by(AuditLog.id))]
        self.assertEqual([a for a, _ in rows], ["operator_created", "password_changed"])
        self.assertNotIn(PASSWORD, str(rows))

    async def test_an_unknown_account_is_an_error_not_a_traceback(self):
        with self.assertRaises(SystemExit):
            await self.run_quiet(operator_cli.passwd("nobody"))


if __name__ == "__main__":
    unittest.main()
