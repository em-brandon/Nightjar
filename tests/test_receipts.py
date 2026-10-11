"""Pushover receipts: asking what became of a priority 2 push, and recording what is learned.

The adapter half is pure (a fake transport). The reconcile half runs against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_receipts -v
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

from hub import migrate, notify, outbox  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import Client, Issue, IssueAction, Notification, Site  # noqa: E402

TOKEN = "app-token-SECRET"
USER = "user-key-SECRET"
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


class Fake:
    """A fake Pushover that records requests and answers from a script."""

    def __init__(self, status=200, json=None, raises=None, text=None):
        self.requests: list[httpx.Request] = []
        self.status, self.json, self.raises, self.text = status, json if json is not None else {"status": 1}, raises, text

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raises:
                raise self.raises
            if self.text is not None:
                return httpx.Response(self.status, text=self.text)
            return httpx.Response(self.status, json=self.json)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class CheckReceipt(unittest.TestCase):
    def setUp(self):
        for name, value in (("pushover_token", TOKEN), ("pushover_user", USER)):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    def check(self, fake):
        return run(notify.check_receipt("rcpt1", fake.client()))

    def test_it_asks_for_the_receipt_with_the_token_and_reads_an_acknowledgement(self):
        fake = Fake(json={"status": 1, "acknowledged": 1, "acknowledged_at": 1760000000, "expired": 0})
        state = self.check(fake)
        self.assertEqual(fake.requests[0].method, "GET")
        self.assertIn("/receipts/rcpt1.json", str(fake.requests[0].url))
        self.assertEqual(fake.requests[0].url.params["token"], TOKEN)
        self.assertEqual((state.acknowledged, state.expired), (True, False))
        self.assertEqual(state.acknowledged_at.timestamp(), 1760000000)

    def test_it_reads_a_receipt_that_expired_unanswered(self):
        state = self.check(Fake(json={"status": 1, "acknowledged": 0, "acknowledged_at": 0, "expired": 1}))
        self.assertEqual((state.acknowledged, state.expired, state.acknowledged_at), (False, True, None))

    def test_a_receipt_still_in_flight_is_neither(self):
        state = self.check(Fake(json={"status": 1, "acknowledged": 0, "expired": 0}))
        self.assertEqual((state.acknowledged, state.expired), (False, False))

    def test_failures_are_classified_and_never_carry_the_token(self):
        cases = (
            (Fake(status=500, json={}), True),
            (Fake(status=429, json={}), True),
            (Fake(raises=httpx.ConnectError(f"https://api.pushover.net/1/receipts/x.json?token={TOKEN}")), True),
            (Fake(status=400, json={"status": 0, "errors": ["receipt not found"]}), False),
            (Fake(status=200, text="<html>"), False),
        )
        for fake, retryable in cases:
            with self.assertRaises(notify.SendError) as ctx:
                self.check(fake)
            self.assertEqual(ctx.exception.retryable, retryable)
            self.assertNotIn(TOKEN, str(ctx.exception))

    def test_it_refuses_without_calling_out_when_not_configured(self):
        fake = Fake()
        with patch.object(settings, "pushover_token", ""):
            with self.assertRaises(notify.NotConfigured):
                self.check(fake)
        self.assertEqual(fake.requests, [])


class CancelReceipt(unittest.TestCase):
    def setUp(self):
        for name, value in (("pushover_token", TOKEN), ("pushover_user", USER)):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    def cancel(self, fake):
        return run(notify.cancel_receipt("rcpt1", fake.client()))

    def test_it_posts_the_token_to_the_receipts_cancel_url(self):
        fake = Fake(json={"status": 1, "request": "r"})
        self.assertTrue(self.cancel(fake).confirmed)
        req = fake.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertTrue(str(req.url).endswith("/1/receipts/rcpt1/cancel.json"))
        self.assertIn(f"token={TOKEN}", req.content.decode())

    def test_a_refusal_is_a_settled_answer_not_a_reason_to_retry_forever(self):
        # the receipt had already expired, been acknowledged, or was not known: nothing is repeating
        self.assertFalse(self.cancel(Fake(status=400, json={"status": 0, "errors": ["receipt not found"]})).confirmed)
        self.assertFalse(self.cancel(Fake(status=200, json={"status": 0})).confirmed)
        self.assertFalse(self.cancel(Fake(status=200, text="not json")).confirmed)

    def test_a_failure_that_may_clear_is_raised_as_retryable_and_never_carries_the_token(self):
        for fake in (
            Fake(status=500, json={}),
            Fake(status=429, json={}),
            Fake(raises=httpx.ReadTimeout("slow")),
            Fake(raises=httpx.ConnectError(f"https://api.pushover.net/1/receipts/x/cancel.json?token={TOKEN}")),
        ):
            with self.assertRaises(notify.SendError) as ctx:
                self.cancel(fake)
            self.assertTrue(ctx.exception.retryable)
            self.assertNotIn(TOKEN, str(ctx.exception))

    def test_it_refuses_without_calling_out_when_not_configured(self):
        fake = Fake()
        with patch.object(settings, "pushover_token", ""):
            with self.assertRaises(notify.NotConfigured):
                self.cancel(fake)
        self.assertEqual(fake.requests, [])


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


def returning(state=None, error=None):
    """A check function that records what it was asked and answers with a fixed state or error."""
    calls: list[str] = []

    async def check(receipt):
        calls.append(receipt)
        if error:
            raise error
        return state

    check.calls = calls
    return check


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Reconcile(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_receipts_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, issues, notifications, issue_actions RESTART IDENTITY CASCADE"))
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            site = Site(client_id=client.id, name="acme-shop")
            session.add(site)
            await session.flush()
            issue = Issue(site_id=site.id, fingerprint="f" * 32, title="Boom", severity="P1")
            session.add(issue)
            await session.commit()
            self.issue_id = issue.id
        self._n = 0

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add(self, **over) -> int:
        self._n += 1
        values = dict(
            issue_id=self.issue_id, change="new", changed_at=T0 + timedelta(seconds=self._n), severity="P1", priority=2,
            site="acme-shop", title="Boom", status="sent", attempts=1, next_attempt_at=T0, sent_at=T0, receipt=f"r{self._n}",
        )
        values.update(over)
        async with self.maker() as session:
            row = Notification(**values)
            session.add(row)
            await session.commit()
            return row.id

    async def row(self, id_):
        async with self.maker() as session:
            return await session.get(Notification, id_)

    async def acks(self):
        async with self.maker() as session:
            return list((await session.scalars(select(IssueAction).where(IssueAction.action == "ack"))).all())

    async def test_an_acknowledgement_the_callback_missed_is_recorded_once(self):
        id_ = await self.add()
        check = returning(notify.ReceiptState(True, T0 + timedelta(minutes=2), False))
        self.assertEqual(await outbox.reconcile_receipts(self.maker, check, now=T0 + timedelta(minutes=3)), 1)
        self.assertEqual((await self.row(id_)).acked_at, T0 + timedelta(minutes=2))
        self.assertEqual([a.detail for a in await self.acks()], ["phone"])
        # a second pass has nothing left to ask about
        self.assertEqual(await outbox.reconcile_receipts(self.maker, check, now=T0 + timedelta(minutes=4)), 0)
        self.assertEqual(len(check.calls), 1)
        self.assertEqual(len(await self.acks()), 1)

    async def test_a_receipt_that_expired_unanswered_is_stamped_and_not_acknowledged(self):
        id_ = await self.add()
        now = T0 + timedelta(minutes=31)
        await outbox.reconcile_receipts(self.maker, returning(notify.ReceiptState(False, None, True)), now=now)
        row = await self.row(id_)
        self.assertEqual((row.expired_at, row.acked_at), (now, None))
        self.assertEqual(await self.acks(), [])

    async def test_a_receipt_still_in_flight_is_asked_about_again_on_the_next_pass(self):
        id_ = await self.add()
        check = returning(notify.ReceiptState(False, None, False))
        await outbox.reconcile_receipts(self.maker, check, now=T0 + timedelta(minutes=5))
        row = await self.row(id_)
        self.assertEqual((row.acked_at, row.expired_at), (None, None))
        await outbox.reconcile_receipts(self.maker, check, now=T0 + timedelta(minutes=6))
        self.assertEqual(len(check.calls), 2)

    async def test_a_failed_check_changes_nothing(self):
        id_ = await self.add()
        await outbox.reconcile_receipts(self.maker, returning(error=notify.SendError("Pushover timed out", True)), now=T0)
        row = await self.row(id_)
        self.assertEqual((row.acked_at, row.expired_at, row.status), (None, None, "sent"))

    async def test_only_sent_unsettled_receipts_are_asked_about(self):
        await self.add(status="pending", receipt="r-pending")
        await self.add(status="failed", receipt="r-failed")
        await self.add(receipt=None)  # a normal push has no receipt
        await self.add(receipt="r-acked", acked_at=T0)
        await self.add(receipt="r-expired", expired_at=T0)
        await self.add(receipt="r-ancient", sent_at=T0 - timedelta(days=2))
        await self.add(receipt="r-wanted")
        check = returning(notify.ReceiptState(False, None, False))
        await outbox.reconcile_receipts(self.maker, check, now=T0 + timedelta(hours=1))
        self.assertEqual(check.calls, ["r-wanted"])

    async def test_it_stops_quietly_when_pushover_is_not_configured(self):
        await self.add()
        check = returning(error=notify.NotConfigured("Pushover is not configured"))
        self.assertEqual(await outbox.reconcile_receipts(self.maker, check, now=T0), 0)


if __name__ == "__main__":
    unittest.main()
