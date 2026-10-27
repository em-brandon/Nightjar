"""Every Python sender signs the way the hub checks, and its reports expire.

    python -m unittest tests.test_signing_contract -v

No database. Each sender builds a real request (the network call is captured, not made) and the hub's own _verify
decides whether it passes, so the two halves are tested against each other and not against a copy of the format.
The Node, PHP and Java reporters are proven against a running hub in tests/test_server_reporters.py.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

# thermal_probe reads its settings when it is imported
os.environ.setdefault("NIGHTJAR_INGEST_URL", "https://nightjar.example/ingest/K/event")
os.environ.setdefault("NIGHTJAR_INGEST_SECRET", "s3cret")

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import home_reporter  # noqa: E402
import jellyfin_reporter  # noqa: E402
import reporter  # noqa: E402
import stack_reporter  # noqa: E402
import thermal_probe  # noqa: E402
from hub import ingest  # noqa: E402
from hub.config import settings  # noqa: E402

SECRET = "contract-secret"


def captured_urllib(send):
    """Run `send()` with urlopen replaced, and return the (headers, body) of the one request it made."""
    seen = {}

    def fake(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = req.data

        class Resp:
            def read(self):
                return b""

        return Resp()

    with patch("urllib.request.urlopen", fake):
        send()
    return seen["headers"], seen["body"]


def python_reporter_request():
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("it broke")

    reporter.install_reporter(app, url="https://nightjar.example/ingest/K/event", secret=SECRET)
    post = AsyncMock()

    async def drain():
        pending = list(app.state.nightjar_in_flight)
        if pending:  # a send that already finished has left the set
            await asyncio.wait(pending, timeout=5)

    with patch.object(httpx.AsyncClient, "post", post):
        with TestClient(app, raise_server_exceptions=False) as client:
            client.get("/boom")
            client.portal.call(drain)
    kwargs = post.await_args.kwargs
    return {k.lower(): v for k, v in kwargs["headers"].items()}, kwargs["content"]


def thermal_request():
    with patch.object(thermal_probe, "INGEST_URL", "https://nightjar.example/ingest/K/event"), patch.object(
        thermal_probe, "INGEST_SECRET", SECRET
    ):
        return captured_urllib(lambda: thermal_probe.report("ThermalWarning", "CPU running hot", "error", {}))


SENDERS = {
    "reporter.py": python_reporter_request,
    "home_reporter": lambda: captured_urllib(lambda: home_reporter.post("https://nightjar.example", "K", SECRET, {"a": 1})),
    "jellyfin_reporter": lambda: captured_urllib(lambda: jellyfin_reporter.post("https://nightjar.example", "K", SECRET, {"a": 1})),
    "stack_reporter": lambda: captured_urllib(lambda: stack_reporter.post("https://nightjar.example", "K", SECRET, {"a": 1})),
    "thermal_probe": thermal_request,
}


def status(headers, body, *, require=False, now_offset=0):
    """What the hub answers to this request, `now_offset` seconds after it was made."""
    import time

    real = time.time
    with patch.object(settings, "verify_signatures", True), patch.object(settings, "require_signed_timestamp", require), patch(
        "hub.ingest.time.time", lambda: real() + now_offset
    ):
        try:
            ingest._verify(SECRET, body, headers.get("x-hub-signature"), headers.get("x-hub-timestamp"))
        except HTTPException as e:
            return e.status_code
    return 200


class Contract(unittest.TestCase):
    def test_every_sender_passes_the_hub_even_when_a_timestamp_is_required(self):
        for name, send in SENDERS.items():
            headers, body = send()
            self.assertIn("x-hub-timestamp", headers, name)
            self.assertEqual(status(headers, body, require=True), 200, name)

    def test_a_captured_report_is_refused_once_it_is_old(self):
        for name, send in SENDERS.items():
            headers, body = send()
            self.assertEqual(status(headers, body, now_offset=60), 200, f"{name} a minute later")
            self.assertEqual(status(headers, body, now_offset=3600), 401, f"{name} an hour later")

    def test_a_tampered_report_is_refused(self):
        for name, send in SENDERS.items():
            headers, body = send()
            self.assertEqual(status(headers, body + b" "), 401, name)


if __name__ == "__main__":
    unittest.main()
