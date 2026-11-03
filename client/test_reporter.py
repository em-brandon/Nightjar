"""The Python reporter's rules: it never breaks the app, and it refuses a plain http:// hub out loud.

    python -m unittest discover -s client
"""

import asyncio
import logging
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import reporter  # noqa: E402


def app_with(url: str) -> FastAPI:
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("it broke")

    reporter.install_reporter(app, url=url, secret="s3cret")
    return app


async def _drain(app: FastAPI) -> None:
    pending = list(app.state.nightjar_in_flight)
    if pending:
        await asyncio.wait(pending, timeout=5)


def hit(app: FastAPI):
    """One failing request. Reports are sent in the background, so keep the app's event loop alive until they finish;
    without that the test client would tear the loop down and cancel them."""
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom")
        client.portal.call(_drain, app)
    return resp


class HubRefusal(unittest.TestCase):
    def test_https_and_http_to_this_machine_are_fine(self):
        for ok in (
            "https://nightjar.example/ingest/K/event",
            "http://localhost:8000/ingest/K/event",
            "http://127.0.0.1:9/ingest/K/event",
            "http://[::1]:8000/ingest/K/event",
            "http://app.localhost/ingest/K/event",
        ):
            self.assertIsNone(reporter.hub_refusal(ok), ok)

    def test_plain_http_elsewhere_and_anything_odd_is_refused(self):
        for bad in (
            "http://nightjar.example/ingest/K/event",
            "http://10.0.0.5/ingest/K/event",
            "http://192.168.1.20:8000/ingest/K/event",
            "http://hub.test/ingest/K/event",
            "ftp://nightjar.example/ingest/K/event",
            "nightjar.example/ingest/K/event",
            "//nightjar.example/ingest/K/event",
            "",
        ):
            self.assertIsNotNone(reporter.hub_refusal(bad), bad)

    def test_the_reason_names_the_host_but_never_the_whole_address(self):
        why = reporter.hub_refusal("http://user:pa55word@nightjar.example/ingest/SECRETKEY/event?token=abc")
        self.assertIn("http://nightjar.example", why)
        for leak in ("pa55word", "SECRETKEY", "token=abc", "user:"):
            self.assertNotIn(leak, why)


class Reporting(unittest.TestCase):
    def test_a_refused_hub_logs_once_when_installed_and_never_sends(self):
        post = AsyncMock()
        with patch.object(httpx.AsyncClient, "post", post), self.assertLogs("nightjar", level=logging.ERROR) as logs:
            app = app_with("http://nightjar.example/ingest/K/event")
            first, second = hit(app), hit(app)
        self.assertEqual(len(logs.records), 1, "said once, at install, and not again on every error")
        self.assertIn("refusing to report errors to http://nightjar.example", logs.output[0])
        post.assert_not_called()
        # the app behaves exactly as it does without the reporter
        self.assertEqual((first.status_code, second.status_code), (500, 500))
        self.assertEqual(first.json(), {"detail": "internal server error"})

    def test_a_good_hub_still_reports_and_is_not_warned_about(self):
        for url in ("https://nightjar.example/ingest/K/event", "http://127.0.0.1:8000/ingest/K/event", "http://localhost:8000/ingest/K/event"):
            post = AsyncMock()
            with patch.object(httpx.AsyncClient, "post", post), self.assertNoLogs("nightjar", level=logging.ERROR):
                resp = hit(app_with(url))
            self.assertEqual(resp.status_code, 500, url)
            post.assert_awaited_once()
            self.assertEqual(post.await_args.args[0], url)
            self.assertTrue(post.await_args.kwargs["headers"]["X-Hub-Signature"].startswith("sha256="))

    def test_a_hub_that_is_down_does_not_change_what_a_crash_does(self):
        post = AsyncMock(side_effect=httpx.ConnectError("down"))
        with patch.object(httpx.AsyncClient, "post", post):
            resp = hit(app_with("https://nightjar.example/ingest/K/event"))
        self.assertEqual((resp.status_code, resp.json()), (500, {"detail": "internal server error"}))


class Safety(unittest.TestCase):
    """Reporting must not make the app slower or heavier, however the hub behaves."""

    def test_a_slow_hub_does_not_slow_the_error_response(self):
        async def slow(*_a, **_k):
            await asyncio.sleep(2)  # the hub taking its full time to answer

        app = app_with("https://nightjar.example/ingest/K/event")
        with patch.object(httpx.AsyncClient, "post", slow):
            with TestClient(app, raise_server_exceptions=False) as client:
                started = time.monotonic()
                resp = client.get("/boom")
                took = time.monotonic() - started
                for task in list(app.state.nightjar_in_flight):
                    task.cancel()  # do not make the test wait the 2 seconds out
        self.assertEqual(resp.status_code, 500)
        self.assertLess(took, 1.0, f"the response waited on the hub ({took:.2f}s)")

    def test_an_error_storm_is_capped_by_sends_in_flight(self):
        calls = []

        async def hang(*a, **k):
            calls.append(a)
            await asyncio.sleep(30)

        app = FastAPI()

        @app.get("/boom")
        def boom():
            raise RuntimeError("it broke")

        reporter.install_reporter(app, url="https://nightjar.example/ingest/K/event", secret="s3cret", max_in_flight=3)
        with patch.object(httpx.AsyncClient, "post", hang):
            with TestClient(app, raise_server_exceptions=False) as client:
                codes = [client.get("/boom").status_code for _ in range(12)]
                client.portal.call(asyncio.sleep, 0.05)
                for task in list(app.state.nightjar_in_flight):
                    task.cancel()
        self.assertEqual(codes, [500] * 12, "every request still got its normal answer")
        self.assertEqual(len(calls), 3, "only max_in_flight sends were ever started; the rest were dropped")

    def test_an_error_storm_is_capped_per_minute_and_recovers(self):
        now = [1000.0]
        post = AsyncMock()
        app = FastAPI()

        @app.get("/boom")
        def boom():
            raise RuntimeError("it broke")

        reporter.install_reporter(app, url="https://nightjar.example/ingest/K/event", secret="s3cret", max_per_minute=4)
        with patch.object(httpx.AsyncClient, "post", post), patch.object(reporter, "_clock", lambda: now[0]):
            with TestClient(app, raise_server_exceptions=False) as client:
                for _ in range(10):
                    client.get("/boom")
                    client.portal.call(_drain, app)
                self.assertEqual(post.await_count, 4, "10 errors in a minute, 4 reported")
                now[0] += 61
                client.get("/boom")
                client.portal.call(_drain, app)
                self.assertEqual(post.await_count, 5, "a minute later reporting resumes")

    def test_an_exception_that_cannot_describe_itself_still_gets_its_500(self):
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("str() is broken")

        app = FastAPI()

        @app.get("/boom")
        def boom():
            raise Unprintable()

        post = AsyncMock()
        reporter.install_reporter(app, url="https://nightjar.example/ingest/K/event", secret="s3cret")
        with patch.object(httpx.AsyncClient, "post", post):
            resp = TestClient(app, raise_server_exceptions=False).get("/boom")
        self.assertEqual((resp.status_code, resp.json()), (500, {"detail": "internal server error"}))
        post.assert_not_called()


class ExistingHandlerTests(unittest.TestCase):
    URL = "https://nightjar.example/ingest/K/event"

    def hit(self, app: FastAPI):
        @app.get("/boom")
        def boom():
            raise RuntimeError("kaboom")

        post = AsyncMock()
        with patch.object(httpx.AsyncClient, "post", post):
            resp = TestClient(app, raise_server_exceptions=False).get("/boom")
        return resp, post

    def test_the_apps_own_catch_all_handler_still_answers_and_the_error_is_still_reported(self):
        app = FastAPI()

        @app.exception_handler(Exception)
        async def mine(request, exc):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": {"code": "E500"}}, status_code=500)

        reporter.install_reporter(app, url=self.URL, secret="s3cret")
        resp, post = self.hit(app)
        self.assertEqual(resp.json(), {"error": {"code": "E500"}})
        post.assert_called_once()

    def test_a_sync_handler_registered_for_500_is_kept_too(self):
        app = FastAPI()

        def mine(request, exc):
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("custom", status_code=503)

        app.add_exception_handler(500, mine)
        reporter.install_reporter(app, url=self.URL, secret="s3cret")
        resp, post = self.hit(app)
        self.assertEqual((resp.status_code, resp.text), (503, "custom"))
        post.assert_called_once()

    def test_installing_twice_reports_once(self):
        app = FastAPI()
        reporter.install_reporter(app, url=self.URL, secret="s3cret")
        reporter.install_reporter(app, url=self.URL, secret="s3cret")
        resp, post = self.hit(app)
        self.assertEqual(resp.status_code, 500)
        post.assert_called_once()


class ScrubTests(unittest.TestCase):
    def test_scrub_removes_secrets_and_leaves_ordinary_text(self):
        out = reporter.scrub("login failed for bob@example.com with password=hunter2 via https://u:pw@db.internal/x, Authorization: Bearer abcdef123456789")
        for leaked in ("hunter2", "u:pw", "abcdef123456789", "bob@"):
            self.assertNotIn(leaked, out)
        self.assertEqual(reporter.scrub("cart is empty for order 42"), "cart is empty for order 42")

    def test_frames_are_relative_and_their_source_line_is_scrubbed(self):
        try:
            password = "x"  # noqa: F841
            raise ValueError("token=abc123 failed")
        except ValueError as e:
            frames = reporter._frames(e)
        self.assertFalse(os.path.isabs(frames[-1]["file"]), frames[-1]["file"])
        self.assertNotIn(os.getcwd().replace("\\", "/"), frames[-1]["file"])


if __name__ == "__main__":
    unittest.main()
