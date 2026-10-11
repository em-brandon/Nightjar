"""Pushover's acknowledgement callback, against a real throwaway database.

    docker compose -f docker-compose.dev.yml up -d db
    python -m unittest tests.test_ack -v
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import pool, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from hub import migrate  # noqa: E402
from hub.api import acknowledged_ids  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402
from hub.models import Client, Issue, IssueAction, Notification, Site  # noqa: E402

TOKEN = "cb-token-0123456789"
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


@unittest.skipUnless(_reachable(), "Postgres is not reachable (start the dev database container)")
class Ack(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = f"nj_ack_{uuid.uuid4().hex[:10]}"
        asyncio.run(_admin(f'CREATE DATABASE "{cls.name}"'))
        cls.url = make_url(settings.database_url).set(database=cls.name).render_as_string(hide_password=False)
        migrate.upgrade(cls.url)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    async def asyncSetUp(self):
        p = patch.object(settings, "pushover_callback_token", TOKEN)
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
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.engine.dispose()

    async def alert(self, status="open", receipt="rcpt-1") -> tuple[int, int]:
        async with self.maker() as session:
            client = Client(name="Acme")
            session.add(client)
            await session.flush()
            site = Site(client_id=client.id, name="acme-shop")
            session.add(site)
            await session.flush()
            issue = Issue(site_id=site.id, fingerprint=uuid.uuid4().hex, title="Boom", severity="P1", status=status)
            session.add(issue)
            await session.flush()
            note = Notification(
                issue_id=issue.id, change="new", changed_at=T0, severity="P1", priority=2, site="acme-shop",
                title="Boom", status="sent", attempts=1, next_attempt_at=T0, receipt=receipt,
            )
            session.add(note)
            await session.commit()
            return issue.id, note.id

    def post(self, data, token=TOKEN):
        return self.http.post(f"/pushover/ack/{token}", data=data)

    async def acks(self, issue_id):
        async with self.maker() as session:
            return list((await session.scalars(select(IssueAction).where(IssueAction.issue_id == issue_id, IssueAction.action == "ack"))).all())

    async def note(self, id_):
        async with self.maker() as session:
            return await session.get(Notification, id_)

    # ---------------------------------------------------------------- who may call

    async def test_with_no_token_configured_everything_is_refused(self):
        with patch.object(settings, "pushover_callback_token", ""):
            self.assertEqual((await self.post({"receipt": "x", "acknowledged": "1"}, token="anything")).status_code, 401)
            self.assertEqual((await self.post({"receipt": "x"}, token="")).status_code, 404)  # no path left to match

    async def test_a_wrong_token_is_refused_and_changes_nothing(self):
        issue_id, note_id = await self.alert()
        r = await self.post({"receipt": "rcpt-1", "acknowledged": "1"}, token="not-the-token")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(await self.acks(issue_id), [])
        self.assertIsNone((await self.note(note_id)).acked_at)

    # ---------------------------------------------------------------- what it does

    async def test_an_acknowledgement_stamps_the_alert_and_acknowledges_the_incident(self):
        issue_id, note_id = await self.alert()
        r = await self.post({"receipt": "rcpt-1", "acknowledged": "1", "acknowledged_by": "u123"})
        self.assertEqual((r.status_code, r.json()), (200, {"matched": True}))
        self.assertIsNotNone((await self.note(note_id)).acked_at)
        (action,) = await self.acks(issue_id)
        self.assertEqual(action.detail, "phone")
        async with self.maker() as session:
            self.assertIn(issue_id, await acknowledged_ids(session, [issue_id]))

    async def test_the_same_callback_twice_acknowledges_once(self):
        issue_id, _ = await self.alert()
        await self.post({"receipt": "rcpt-1", "acknowledged": "1"})
        await self.post({"receipt": "rcpt-1", "acknowledged": "1"})
        self.assertEqual(len(await self.acks(issue_id)), 1)

    async def test_an_unknown_receipt_is_answered_but_writes_nothing(self):
        issue_id, note_id = await self.alert()
        r = await self.post({"receipt": "someone-elses", "acknowledged": "1"})
        self.assertEqual((r.status_code, r.json()), (200, {"matched": False}))
        self.assertEqual(await self.acks(issue_id), [])
        self.assertIsNone((await self.note(note_id)).acked_at)

    async def test_a_callback_with_no_receipt_is_harmless(self):
        r = await self.post({"acknowledged": "1"})
        self.assertEqual((r.status_code, r.json()), (200, {"matched": False}))

    async def test_a_receipt_that_expired_unanswered_is_not_an_acknowledgement(self):
        issue_id, note_id = await self.alert()
        await self.post({"receipt": "rcpt-1", "acknowledged": "0", "expired": "1"})
        self.assertEqual(await self.acks(issue_id), [])
        self.assertIsNone((await self.note(note_id)).acked_at)

    async def test_an_issue_that_is_no_longer_open_is_stamped_but_not_acknowledged(self):
        issue_id, note_id = await self.alert(status="resolved")
        await self.post({"receipt": "rcpt-1", "acknowledged": "1"})
        self.assertIsNotNone((await self.note(note_id)).acked_at)
        self.assertEqual(await self.acks(issue_id), [])


if __name__ == "__main__":
    unittest.main()
