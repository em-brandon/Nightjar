"""Hostile or sloppy input must be a 4xx, never a 500 (security audit finding 4).

    python -m unittest tests.test_malformed_input -v

A 500 tells a caller the server tripped on something they sent, and makes a retrying reporter retry. No database: the pieces
that would need one are replaced.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from hub import credentials, ingest  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.db import get_session  # noqa: E402
from hub.main import app  # noqa: E402


def client() -> httpx.AsyncClient:
    # raise_app_exceptions off: an unhandled error must show up as a 500 response, which is what a caller would see
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


async def no_session():
    yield None


class Totp(unittest.TestCase):
    def test_digits_of_other_scripts_are_not_a_code_and_do_not_raise(self):
        secret = b"k" * 20
        for code in ("١٢٣٤٥٦", "１２３４５６", "123４56", "²²²²²²"):
            self.assertEqual(len(code), 6)
            self.assertIsNone(credentials.totp_match(secret, code, 0), code)

    def test_an_ordinary_code_still_works(self):
        secret = b"k" * 20
        step = credentials.totp_step()
        code = credentials._code(secret, step)
        self.assertEqual(credentials.totp_match(secret, code, 0), step)


class Presence(unittest.IsolatedAsyncioTestCase):
    async def test_a_non_ascii_token_is_a_401_not_a_500(self):
        app.dependency_overrides[get_session] = no_session
        self.addCleanup(app.dependency_overrides.clear)
        with patch.object(settings, "presence_token", "right-token-0123456789"):
            async with client() as c:
                for token, expected in (("wrong-é", 401), ("٣٤٥", 401), ("😀", 401), ("x" * 5000, 422)):
                    r = await c.post("/presence", json={"state": "home", "ttl_minutes": 60, "source": "nfc", "token": token})
                    self.assertEqual(r.status_code, expected, token[:10])


class IngestBody(unittest.IsolatedAsyncioTestCase):
    async def test_a_signed_but_malformed_report_is_a_422_that_echoes_nothing_back(self):
        app.dependency_overrides[get_session] = no_session
        self.addCleanup(app.dependency_overrides.clear)
        site = SimpleNamespace(id=1)
        with patch.object(ingest, "_load_site", AsyncMock(return_value=site)), patch.object(ingest, "verify_and_note", AsyncMock()):
            async with client() as c:
                for body in (b"not json at all", b"[]", b'{"level":"error"}', b'{"exception_type":"X","level":"catastrophic"}',
                             b'{"exception_type":"X","frames":"nope"}'):
                    r = await c.post("/ingest/KEY/event", content=body)
                    self.assertEqual(r.status_code, 422, body)
                secret_looking = b'{"exception_type":"X","level":"hunter2-sk_live_123"}'
                r = await c.post("/ingest/KEY/event", content=secret_looking)
                self.assertEqual(r.status_code, 422)
                self.assertNotIn("sk_live_123", r.text, "the rejected value is not sent back")


if __name__ == "__main__":
    unittest.main()
