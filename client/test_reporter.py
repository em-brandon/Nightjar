"""The Python reporter's rules: it never breaks the app, and it refuses a plain http:// hub out loud.

    python -m unittest discover -s client
"""

import logging
import os
import sys
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


def hit(app: FastAPI):
    return TestClient(app, raise_server_exceptions=False).get("/boom")


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


if __name__ == "__main__":
    unittest.main()
