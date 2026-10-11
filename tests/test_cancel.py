"""Cancelling a repeating push once the incident it is about has been dealt with.

If you acknowledge or resolve a P1 at your desk, the phone must stop alarming. The action stamps the incident's
sent alerts as dismissed in the same transaction, and the sender then tells Pushover to cancel them, retrying
until it has. Against a real throwaway database, with a fake Pushover.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_cancel -v
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

from hub import acks, migrate, notify, outbox  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402
from hub.models import Client, Issue, IssueAction, Notification, Site  # noqa: E402

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


class FakeCancel:
    """A cancel function that records what it was asked and answers from a script."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    async def __call__(self, receipt):
        self.calls.append(receipt)
        step = self.script.pop(0) if self.script else notify.Cancelled(confirmed=True)
        if isinstance(step, Exception):
            raise step
        return step


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Cancel(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_cancel_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        for name, value in (("console_token", ""), ("pushover_token", "tok"), ("pushover_user", "usr")):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.engine = create_async_engine(self.url, pool_size=5)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override():
            async with self.maker() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE clients, sites, issues, notifications, issue_actions RESTART IDENTITY CASCADE"))
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            site = Site(client_id=client.id, name="acme-shop")
            session.add(site)
            await session.flush()
            self.site_id = site.id
            self.issue_id = await self._issue(session)
            await session.commit()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self._n = 0

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def _issue(self, session, status="open") -> int:
        issue = Issue(site_id=self.site_id, fingerprint=uuid.uuid4().hex, title="Boom", severity="P1", status=status)
        session.add(issue)
        await session.flush()
        return issue.id

    async def add(self, issue_id=None, **over) -> int:
        self._n += 1
        values = dict(
            issue_id=issue_id or self.issue_id, change="new", changed_at=T0 + timedelta(seconds=self._n), severity="P1", priority=2,
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

    async def act(self, action, **body):
        r = await self.http.post(f"/api/issues/{self.issue_id}/actions", json={"action": action, **body})
        self.assertEqual(r.status_code, 200, r.text)

    # ---------------------------------------------------------------- the helper

    async def test_it_dismisses_only_the_sent_alerts_of_that_incident(self):
        sent = await self.add()
        failed = await self.add(status="failed", receipt=None)
        pending = await self.add(status="pending", receipt=None)
        async with self.maker() as session:
            other = await self._issue(session)
            await session.commit()
        elsewhere = await self.add(issue_id=other)
        async with self.maker() as session:
            self.assertEqual(await acks.dismiss_alerts(session, self.issue_id, T0), 1)
            await session.commit()
        self.assertEqual((await self.row(sent)).dismissed_at, T0)
        for id_ in (failed, pending, elsewhere):
            self.assertIsNone((await self.row(id_)).dismissed_at, "a failed or queued alert, or another incident's, is left alone")

    async def test_it_is_idempotent_and_can_skip_one_alert(self):
        a = await self.add()
        b = await self.add()
        async with self.maker() as session:
            self.assertEqual(await acks.dismiss_alerts(session, self.issue_id, T0, except_id=a), 1)
            self.assertEqual(await acks.dismiss_alerts(session, self.issue_id, T0 + timedelta(hours=1)), 1, "only the one left")
            self.assertEqual(await acks.dismiss_alerts(session, self.issue_id), 0, "nothing left to mark")
            await session.commit()
        self.assertEqual((await self.row(b)).dismissed_at, T0)
        self.assertEqual((await self.row(a)).dismissed_at, T0 + timedelta(hours=1))

    # ---------------------------------------------------------------- the cancel pass

    async def test_a_dismissed_push_is_cancelled_once(self):
        id_ = await self.add(dismissed_at=T0)
        cancel = FakeCancel()
        self.assertEqual(await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(minutes=1)), 1)
        self.assertEqual(cancel.calls, [f"r{self._n}"])
        self.assertEqual((await self.row(id_)).cancelled_at, T0 + timedelta(minutes=1))
        self.assertEqual(await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(minutes=2)), 0)
        self.assertEqual(len(cancel.calls), 1, "asked once, not every pass")

    async def test_a_push_nobody_dealt_with_is_left_to_repeat(self):
        await self.add()  # not dismissed
        cancel = FakeCancel()
        await outbox.cancel_receipts(self.maker, cancel, now=T0)
        self.assertEqual(cancel.calls, [])

    async def test_nothing_is_cancelled_that_is_already_acknowledged_expired_or_has_no_receipt(self):
        await self.add(dismissed_at=T0, acked_at=T0)
        await self.add(dismissed_at=T0, expired_at=T0)
        await self.add(dismissed_at=T0, receipt=None)  # a normal push: it does not repeat
        await self.add(dismissed_at=T0, status="failed")
        cancel = FakeCancel()
        await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(minutes=1))
        self.assertEqual(cancel.calls, [])

    async def test_a_failed_cancel_is_tried_again_until_it_works(self):
        id_ = await self.add(dismissed_at=T0)
        cancel = FakeCancel(notify.SendError("Pushover timed out", True), notify.SendError("Pushover answered 503", True))
        await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(seconds=5))
        self.assertIsNone((await self.row(id_)).cancelled_at, "still alarming: not marked cancelled")
        await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(seconds=10))
        self.assertIsNone((await self.row(id_)).cancelled_at)
        await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(seconds=15))
        self.assertEqual((await self.row(id_)).cancelled_at, T0 + timedelta(seconds=15))
        self.assertEqual(len(cancel.calls), 3)

    async def test_a_refused_receipt_is_settled_so_it_is_not_asked_about_forever(self):
        id_ = await self.add(dismissed_at=T0)
        cancel = FakeCancel(notify.Cancelled(confirmed=False))
        await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(minutes=1))
        self.assertIsNotNone((await self.row(id_)).cancelled_at)

    async def test_it_stops_quietly_when_pushover_is_not_configured(self):
        id_ = await self.add(dismissed_at=T0)
        cancel = FakeCancel(notify.NotConfigured("Pushover is not configured"))
        self.assertEqual(await outbox.cancel_receipts(self.maker, cancel, now=T0), 0)
        self.assertIsNone((await self.row(id_)).cancelled_at)

    async def test_the_receipt_poll_leaves_a_dismissed_push_alone(self):
        await self.add(dismissed_at=T0)
        calls = []

        async def check(receipt):
            calls.append(receipt)
            return notify.ReceiptState(False, None, True)

        await outbox.reconcile_receipts(self.maker, check, now=T0 + timedelta(minutes=31))
        self.assertEqual(calls, [], "once the incident is handled, an expiry is no longer news")

    # ---------------------------------------------------------------- everything that deals with an incident dismisses

    async def dismissed(self, id_):
        return (await self.row(id_)).dismissed_at is not None

    async def test_acknowledging_in_the_console_dismisses_the_incidents_alerts(self):
        id_ = await self.add()
        await self.act("ack")
        self.assertTrue(await self.dismissed(id_))

    async def test_resolving_muting_or_snoozing_dismisses_them_too(self):
        for action, body in (("resolve", {}), ("mute", {}), ("snooze", {"hours": 2})):
            async with self.maker() as session:
                await session.execute(text("TRUNCATE notifications, issue_actions"))
                await session.execute(text("UPDATE issues SET status = 'open', snoozed_until = NULL"))
                await session.commit()
            id_ = await self.add()
            await self.act(action, **body)
            self.assertTrue(await self.dismissed(id_), action)

    async def test_reopening_does_not_dismiss_anything(self):
        async with self.maker() as session:
            issue = await session.get(Issue, self.issue_id)
            issue.status = "resolved"
            await session.commit()
        id_ = await self.add()
        await self.act("reopen")
        self.assertFalse(await self.dismissed(id_))

    async def test_an_acknowledgement_from_the_phone_stops_the_incidents_other_repeating_pushes(self):
        first = await self.add()
        second = await self.add(change="regression")
        async with self.maker() as session:
            note = await session.get(Notification, first)
            await acks.record_acknowledgement(session, note, T0 + timedelta(minutes=2))
            await session.commit()
        self.assertTrue(await self.dismissed(second), "the other push is still repeating and must be stopped")
        self.assertIsNone((await self.row(first)).dismissed_at, "the acknowledged push itself needs no cancel")

    async def test_the_end_to_end_ack_in_the_console_then_the_phone_is_told_to_stop(self):
        id_ = await self.add()
        await self.act("ack")
        cancel = FakeCancel()
        await outbox.cancel_receipts(self.maker, cancel, now=T0 + timedelta(minutes=1))
        self.assertEqual(cancel.calls, [f"r{self._n}"])
        self.assertIsNotNone((await self.row(id_)).cancelled_at)

    # ---------------------------------------------------------------- the race

    async def sent_with_receipt(self, issue_status="open", acked=False):
        async with self.maker() as session:
            issue = await session.get(Issue, self.issue_id)
            issue.status = issue_status
            if acked:
                session.add(IssueAction(issue_id=issue.id, action="ack", detail=""))
            await session.commit()
        id_ = await self.add(status="pending", attempts=0, receipt=None, sent_at=None, priority=2)

        async def send(n):
            return notify.Sent(receipt="rcpt-new")

        await outbox.run_once(self.maker, send, now=T0)
        return await self.row(id_)

    async def test_a_push_sent_for_an_incident_already_resolved_is_dismissed_at_once(self):
        row = await self.sent_with_receipt(issue_status="resolved")
        self.assertEqual((row.status, row.receipt), ("sent", "rcpt-new"))
        self.assertEqual(row.dismissed_at, T0, "it is on its way and will repeat, so it is stopped")

    async def test_a_push_sent_for_an_incident_already_acknowledged_is_dismissed_at_once(self):
        self.assertIsNotNone((await self.sent_with_receipt(acked=True)).dismissed_at)

    async def test_a_push_for_an_incident_still_open_and_unanswered_is_not_dismissed(self):
        self.assertIsNone((await self.sent_with_receipt()).dismissed_at)


if __name__ == "__main__":
    unittest.main()
