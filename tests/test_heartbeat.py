"""The dead-man's switch ping and the sender loop's survival rules. No database: the pass is patched.

    python -m unittest tests.test_heartbeat -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from hub import outbox  # noqa: E402
from hub.config import settings  # noqa: E402

URL = "https://hc-ping.com/11111111-2222-3333-4444-SECRETSECRET"


def client(status=200, raises=None):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if raises:
            raise raises
        return httpx.Response(status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (("deadman_url", URL), ("deadman_interval_s", 120)):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def test_with_no_url_it_never_calls_out(self):
        http, seen = client()
        with patch.object(settings, "deadman_url", ""):
            self.assertFalse(await outbox.Heartbeat(http).beat(0))
        self.assertEqual(seen, [])

    async def test_it_pings_once_and_then_waits_for_the_interval(self):
        http, seen = client()
        hb = outbox.Heartbeat(http)
        self.assertTrue(await hb.beat(1000))
        self.assertFalse(await hb.beat(1060), "too soon")
        self.assertFalse(await hb.beat(1119))
        self.assertTrue(await hb.beat(1120))
        self.assertEqual(seen, [URL, URL])

    async def test_a_failed_ping_is_retried_on_the_next_pass_not_after_the_interval(self):
        http, _ = client(status=500)
        hb = outbox.Heartbeat(http)
        self.assertFalse(await hb.beat(1000))
        self.assertIsNone(hb.last)
        http2, seen2 = client()
        hb.http = http2
        self.assertTrue(await hb.beat(1001))
        self.assertEqual(len(seen2), 1)

    async def test_a_dead_switch_never_raises_and_the_url_is_never_logged(self):
        http, _ = client(raises=httpx.ConnectError(f"cannot reach {URL}"))
        with self.assertLogs("nightjar.outbox", level="WARNING") as logs:
            self.assertFalse(await outbox.Heartbeat(http).beat(0))
        text = "\n".join(logs.output)
        self.assertIn("ConnectError", text)
        self.assertNotIn("SECRETSECRET", text, "the ping URL is a secret")


class SenderLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p = patch.object(settings, "outbox_poll_s", 0.01)
        p.start()
        self.addCleanup(p.stop)

    async def run_loop(self, passes: int):
        """Run the loop until the patched pass has been called `passes` times, then stop it."""
        done = asyncio.Event()
        calls = 0
        original = outbox.run_once

        async def counting(*a, **k):
            nonlocal calls
            calls += 1
            if calls >= passes:
                done.set()
            return await original(*a, **k)

        task = asyncio.create_task(outbox.sender_loop())
        try:
            with patch.object(outbox, "run_once", counting):
                await asyncio.wait_for(done.wait(), 5)
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_a_healthy_pass_pings_the_switch(self):
        beat = AsyncMock(return_value=True)
        with patch.object(outbox, "run_once", AsyncMock(return_value=0)), patch.object(outbox.Heartbeat, "beat", beat):
            await self.run_loop(3)
        self.assertGreaterEqual(beat.await_count, 1)

    async def test_a_failing_pass_does_not_ping_and_does_not_kill_the_loop(self):
        beat = AsyncMock(return_value=True)
        failing = AsyncMock(side_effect=RuntimeError("the database is down"))
        with patch.object(outbox, "run_once", failing), patch.object(outbox.Heartbeat, "beat", beat):
            with self.assertLogs("nightjar.outbox", level="ERROR"):
                await self.run_loop(3)
        self.assertGreaterEqual(failing.await_count, 3, "the loop kept going after failing")
        self.assertEqual(beat.await_count, 0, "a sender that cannot reach its database must go quiet to the switch")

    async def test_it_recovers_and_pings_again_once_the_pass_works(self):
        beat = AsyncMock(return_value=True)
        flaky = AsyncMock(side_effect=[RuntimeError("blip"), 0, 0, 0, 0])
        with patch.object(outbox, "run_once", flaky), patch.object(outbox.Heartbeat, "beat", beat):
            with self.assertLogs("nightjar.outbox", level="ERROR"):
                await self.run_loop(4)
        self.assertGreaterEqual(beat.await_count, 1)


if __name__ == "__main__":
    unittest.main()
