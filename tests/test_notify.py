"""The Pushover adapter, against a fake transport: nothing here touches the network.

    python -m unittest tests.test_notify -v
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from hub import notify  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import Notification  # noqa: E402

TOKEN = "app-token-SECRET"
USER = "user-key-SECRET"


def note(priority=0, severity="P2", change="new", site="acme-shop", title="KeyError: cart is empty") -> Notification:
    return Notification(severity=severity, change=change, site=site, title=title, priority=priority)


class FakePushover:
    """Records every request and answers with whatever the test sets."""

    def __init__(self, status=200, json=None, raises=None, text=None):
        self.requests: list[httpx.Request] = []
        self.status, self.json, self.raises, self.text = status, json if json is not None else {"status": 1, "request": "r"}, raises, text

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raises:
                raise self.raises
            if self.text is not None:
                return httpx.Response(self.status, text=self.text)
            return httpx.Response(self.status, json=self.json)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def form(self, i=0) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.requests[i].content.decode()).items()}


def run(coro):
    return asyncio.run(coro)


class Configured(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("pushover_token", TOKEN),
            ("pushover_user", USER),
            ("hub_public_url", "https://nightjar.example.co.ke/"),
            ("pushover_callback_token", "cb-secret"),
        ):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    def send(self, n, fake):
        return run(notify.send(n, fake.client()))

    def test_a_normal_push_has_the_incident_in_its_words_and_no_emergency_fields(self):
        fake = FakePushover()
        sent = self.send(note(), fake)
        form = fake.form()
        self.assertEqual((form["token"], form["user"], form["priority"]), (TOKEN, USER, "0"))
        self.assertEqual(form["title"], "P2 acme-shop")
        self.assertEqual(form["message"], "KeyError: cart is empty (new)")
        self.assertEqual(form["url"], "https://nightjar.example.co.ke/#/incidents")
        for field in ("retry", "expire", "callback", "sound"):
            self.assertNotIn(field, form)
        self.assertIsNone(sent.receipt)

    def test_a_p1_is_priority_two_with_retry_expire_sound_and_a_callback(self):
        fake = FakePushover(json={"status": 1, "request": "r", "receipt": "rcpt123"})
        sent = self.send(note(priority=2, severity="P1", change="regression"), fake)
        form = fake.form()
        self.assertEqual((form["priority"], form["retry"], form["expire"]), ("2", "60", "1800"))
        self.assertEqual(form["sound"], "siren")
        self.assertEqual(form["callback"], "https://nightjar.example.co.ke/pushover/ack/cb-secret")
        self.assertEqual(form["message"], "KeyError: cart is empty (is back)")
        self.assertEqual(sent.receipt, "rcpt123")

    def test_a_quiet_push_carries_its_negative_priority(self):
        fake = FakePushover()
        self.send(note(priority=-1), fake)
        self.assertEqual(fake.form()["priority"], "-1")

    def test_without_a_callback_secret_priority_two_still_goes_without_a_callback(self):
        with patch.object(settings, "pushover_callback_token", ""):
            fake = FakePushover()
            self.send(note(priority=2), fake)
        self.assertNotIn("callback", fake.form())

    def test_without_a_public_url_there_is_no_console_link(self):
        with patch.object(settings, "hub_public_url", ""):
            fake = FakePushover()
            self.send(note(), fake)
        self.assertNotIn("url", fake.form())

    def test_long_text_is_cut_to_pushovers_limits(self):
        fake = FakePushover()
        self.send(note(title="x" * 5000, site="s" * 400), fake)
        self.assertLessEqual(len(fake.form()["message"]), 1024)
        self.assertLessEqual(len(fake.form()["title"]), 250)

    # ---------------------------------------------------------------- failures

    def assert_send_error(self, fake, retryable):
        with self.assertRaises(notify.SendError) as ctx:
            self.send(note(), fake)
        self.assertEqual(ctx.exception.retryable, retryable)
        for secret in (TOKEN, USER):
            self.assertNotIn(secret, str(ctx.exception), "a failure message must never carry a credential")
        return str(ctx.exception)

    def test_a_server_error_or_a_rate_limit_may_clear_so_it_is_retryable(self):
        self.assert_send_error(FakePushover(status=500, json={}), True)
        self.assert_send_error(FakePushover(status=503, json={}), True)
        self.assert_send_error(FakePushover(status=429, json={}), True)

    def test_a_timeout_or_a_connection_failure_is_retryable(self):
        self.assert_send_error(FakePushover(raises=httpx.ReadTimeout("slow")), True)
        message = self.assert_send_error(FakePushover(raises=httpx.ConnectError("https://api.pushover.net/1/messages.json?token=" + TOKEN)), True)
        self.assertIn("ConnectError", message)

    def test_a_refused_request_is_not_retryable_and_says_why(self):
        fake = FakePushover(status=400, json={"status": 0, "errors": ["application token is invalid"]})
        message = self.assert_send_error(fake, False)
        self.assertIn("application token is invalid", message)

    def test_a_200_that_says_status_zero_is_a_failure(self):
        self.assert_send_error(FakePushover(status=200, json={"status": 0, "errors": ["user identifier is invalid"]}), False)

    def test_an_answer_that_is_not_json_is_a_failure(self):
        self.assert_send_error(FakePushover(status=200, text="<html>proxy error</html>"), False)
        self.assert_send_error(FakePushover(status=502, text="bad gateway"), True)


class NotConfigured(unittest.TestCase):
    def test_it_refuses_without_calling_the_network(self):
        fake = FakePushover()
        for token, user in (("", ""), (TOKEN, ""), ("", USER)):
            with patch.object(settings, "pushover_token", token), patch.object(settings, "pushover_user", user):
                with self.assertRaises(notify.NotConfigured):
                    run(notify.send(note(), fake.client()))
        self.assertEqual(fake.requests, [], "nothing may be sent while unconfigured")

    def test_configured_needs_both(self):
        with patch.object(settings, "pushover_token", TOKEN), patch.object(settings, "pushover_user", USER):
            self.assertTrue(notify.configured())
        with patch.object(settings, "pushover_token", TOKEN), patch.object(settings, "pushover_user", ""):
            self.assertFalse(notify.configured())


if __name__ == "__main__":
    unittest.main()
