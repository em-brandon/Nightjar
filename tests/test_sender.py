"""The alert outbox's sender: delivery, backoff, giving up visibly, and surviving a crash.
Against a real throwaway database, with a fake send function so nothing touches the network.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_sender -v
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

from hub import migrate, notify, outbox  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import Client, Issue, Notification, Site  # noqa: E402

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


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


class Backoff(unittest.TestCase):
    def test_it_grows_and_then_repeats_the_last_delay(self):
        with patch.object(settings, "outbox_backoff_s", [10, 20, 40]):
            got = [outbox.backoff(n).total_seconds() for n in (1, 2, 3, 4, 9)]
        self.assertEqual(got, [10, 20, 40, 40, 40])

    def test_an_empty_schedule_still_waits(self):
        with patch.object(settings, "outbox_backoff_s", []):
            self.assertGreater(outbox.backoff(1).total_seconds(), 0)


class FakeSend:
    """A send function that answers from a script: Sent, or an exception to raise."""

    def __init__(self, *script, delay=0.0):
        self.script, self.calls, self.delay = list(script), [], delay

    async def __call__(self, n: Notification) -> notify.Sent:
        self.calls.append(n.id)
        if self.delay:
            await asyncio.sleep(self.delay)
        step = self.script.pop(0) if self.script else notify.Sent(receipt=None)
        if isinstance(step, Exception):
            raise step
        return step


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Sender(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_sender_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for name, value in (("outbox_backoff_s", [30, 120, 600]), ("outbox_max_attempts", 4), ("outbox_lease_s", 120)):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=10)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, issues, notifications RESTART IDENTITY CASCADE"))
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            site = Site(client_id=client.id, name="acme-shop")
            session.add(site)
            await session.flush()
            issue = Issue(site_id=site.id, fingerprint="f" * 32, title="Boom", severity="P2")
            session.add(issue)
            await session.commit()
            self.issue_id = issue.id
        self._n = 0

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add(self, **over) -> int:
        self._n += 1
        values = dict(
            issue_id=self.issue_id, change="new", changed_at=T0 + timedelta(seconds=self._n), severity="P2",
            priority=0, site="acme-shop", title="Boom", status="pending", attempts=0, next_attempt_at=T0,
        )
        values.update(over)
        async with self.maker() as session:
            row = Notification(**values)
            session.add(row)
            await session.commit()
            return row.id

    async def row(self, id_: int) -> Notification:
        async with self.maker() as session:
            return await session.get(Notification, id_)

    async def test_a_due_row_is_sent_and_marked_with_its_receipt(self):
        id_ = await self.add()
        send = FakeSend(notify.Sent(receipt="rcpt1"))
        handled = await outbox.run_once(self.maker, send, now=T0)
        row = await self.row(id_)
        self.assertEqual(handled, 1)
        self.assertEqual((row.status, row.attempts, row.receipt, row.sent_at, row.last_error), ("sent", 1, "rcpt1", T0, None))
        self.assertIsNone(row.claimed_at)

    async def test_a_sent_row_is_never_sent_again(self):
        await self.add()
        send = FakeSend()
        await outbox.run_once(self.maker, send, now=T0)
        await outbox.run_once(self.maker, send, now=T0 + timedelta(days=1))
        self.assertEqual(len(send.calls), 1)

    async def test_a_row_that_is_not_due_yet_is_left_alone(self):
        id_ = await self.add(next_attempt_at=T0 + timedelta(minutes=5))
        send = FakeSend()
        self.assertEqual(await outbox.run_once(self.maker, send, now=T0), 0)
        self.assertEqual((await self.row(id_)).status, "pending")
        self.assertEqual(send.calls, [])

    async def test_a_retryable_failure_backs_off_and_then_succeeds(self):
        id_ = await self.add()
        send = FakeSend(notify.SendError("Pushover timed out", True))
        await outbox.run_once(self.maker, send, now=T0)
        row = await self.row(id_)
        self.assertEqual((row.status, row.attempts, row.last_error), ("pending", 1, "Pushover timed out"))
        self.assertEqual(row.next_attempt_at, T0 + timedelta(seconds=30))
        # too early: nothing happens
        self.assertEqual(await outbox.run_once(self.maker, send, now=T0 + timedelta(seconds=29)), 0)
        # due: the second attempt goes through
        await outbox.run_once(self.maker, send, now=T0 + timedelta(seconds=31))
        row = await self.row(id_)
        self.assertEqual((row.status, row.attempts, row.last_error), ("sent", 2, None))

    async def test_each_failure_waits_longer_than_the_last(self):
        id_ = await self.add()
        send = FakeSend(*[notify.SendError("down", True)] * 3)
        now = T0
        waits = []
        for _ in range(3):
            await outbox.run_once(self.maker, send, now=now)
            row = await self.row(id_)
            waits.append((row.next_attempt_at - now).total_seconds())
            now = row.next_attempt_at
        self.assertEqual(waits, [30, 120, 600])

    async def test_it_gives_up_after_the_attempt_limit_and_the_row_stays_failed(self):
        id_ = await self.add()
        send = FakeSend(*[notify.SendError("still down", True)] * 10)
        now = T0
        for _ in range(4):
            await outbox.run_once(self.maker, send, now=now)
            now = (await self.row(id_)).next_attempt_at or now
        row = await self.row(id_)
        self.assertEqual((row.status, row.attempts, row.last_error), ("failed", 4, "still down"))
        # a failed row is not tried again, however long we wait
        before = len(send.calls)
        await outbox.run_once(self.maker, send, now=now + timedelta(days=30))
        self.assertEqual(len(send.calls), before)

    async def test_a_failure_no_retry_can_fix_fails_at_once_and_says_why(self):
        id_ = await self.add()
        await outbox.run_once(self.maker, FakeSend(notify.SendError("Pushover refused it: application token is invalid", False)), now=T0)
        row = await self.row(id_)
        self.assertEqual((row.status, row.attempts), ("failed", 1))
        self.assertIn("token is invalid", row.last_error)

    async def test_not_configured_fails_visibly_instead_of_dropping_the_alert(self):
        id_ = await self.add()
        await outbox.run_once(self.maker, FakeSend(notify.NotConfigured("Pushover is not configured (set PUSHOVER_TOKEN and PUSHOVER_USER)")), now=T0)
        row = await self.row(id_)
        self.assertEqual(row.status, "failed")
        self.assertIn("not configured", row.last_error)

    async def test_with_nothing_configured_the_real_adapter_fails_the_row_visibly(self):
        id_ = await self.add()
        with patch.object(settings, "pushover_token", ""), patch.object(settings, "pushover_user", ""):
            await outbox.run_once(self.maker, notify.send, now=T0)
        row = await self.row(id_)
        self.assertEqual(row.status, "failed")
        self.assertIn("not configured", row.last_error)

    async def test_an_unexpected_error_in_the_adapter_is_retried_not_stuck(self):
        id_ = await self.add()
        await outbox.run_once(self.maker, FakeSend(ValueError("a bug")), now=T0)
        row = await self.row(id_)
        self.assertEqual((row.status, row.attempts, row.last_error), ("pending", 1, "unexpected ValueError"))

    # ---------------------------------------------------------------- crashes

    async def test_a_row_left_sending_by_a_crash_is_taken_again_after_the_lease(self):
        id_ = await self.add(status="sending", attempts=1, claimed_at=T0)
        send = FakeSend()
        self.assertEqual(await outbox.run_once(self.maker, send, now=T0 + timedelta(seconds=60)), 0, "still within the lease")
        self.assertEqual(await outbox.run_once(self.maker, send, now=T0 + timedelta(seconds=121)), 1)
        row = await self.row(id_)
        self.assertEqual((row.status, row.attempts), ("sent", 2))

    async def test_p1_is_sent_before_older_lower_priority_rows(self):
        low = await self.add(priority=-1)
        high = await self.add(priority=2, severity="P1")
        send = FakeSend()
        await outbox.run_once(self.maker, send, now=T0)
        self.assertEqual(send.calls, [high, low])

    async def test_two_senders_never_send_the_same_row_twice(self):
        for _ in range(8):
            await self.add()
        send = FakeSend(delay=0.05)
        a, b = await asyncio.gather(outbox.run_once(self.maker, send, now=T0, limit=4), outbox.run_once(self.maker, send, now=T0, limit=4))
        self.assertEqual(a + b, 8)
        self.assertEqual(sorted(send.calls), list(range(1, 9)), "each row exactly once")
        async with self.maker() as session:
            statuses = (await session.scalars(select(Notification.status))).all()
        self.assertEqual(set(statuses), {"sent"})


if __name__ == "__main__":
    unittest.main()
