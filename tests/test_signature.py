"""How the hub checks an ingest signature.

    python -m unittest tests.test_signature -v

No database: _verify is a pure function of the secret, the body, the header and the settings.
"""

import hashlib
import hmac
import os
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import ingest  # noqa: E402
from hub.config import settings  # noqa: E402

SECRET = "s3cret-for-tests"
BODY = b'{"exception_type":"ValueError"}'


def sign(body: bytes = BODY, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Verify(unittest.TestCase):
    def setUp(self):
        p = patch.object(settings, "verify_signatures", True)
        p.start()
        self.addCleanup(p.stop)

    def status(self, signature):
        try:
            ingest._verify(SECRET, BODY, signature)
        except HTTPException as e:
            return e.status_code
        return 200

    def test_a_good_signature_passes_and_a_wrong_or_missing_one_is_401(self):
        self.assertEqual(self.status(sign()), 200)
        self.assertEqual(self.status(sign(secret="another")), 401)
        self.assertEqual(self.status(sign(b"other body")), 401)
        self.assertEqual(self.status(None), 401)
        self.assertEqual(self.status(""), 401)

    def test_junk_in_the_header_is_a_401_never_a_crash(self):
        # compare_digest raises TypeError on a str with non-ASCII text, which used to surface as a 500
        for junk in ("sha256=é", "☃", "sha256=" + "ÿ" * 64, "\ud800", "\x00"):
            self.assertEqual(self.status(junk), 401, repr(junk))

    def test_nothing_is_checked_when_verification_is_off(self):
        with patch.object(settings, "verify_signatures", False):
            self.assertEqual(self.status("anything at all"), 200)


NOW = 1_800_000_000


def sign_at(timestamp: str, body: bytes = BODY, secret: str = SECRET) -> str:
    """What a reporter that sends X-Hub-Timestamp computes."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


class Replay(unittest.TestCase):
    """A captured request must stop working, and must not be turned into a fresh one."""

    def setUp(self):
        for name, value in (("verify_signatures", True), ("require_signed_timestamp", False), ("signature_max_age_s", 300)):
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)
        t = patch("hub.ingest.time.time", lambda: NOW)
        t.start()
        self.addCleanup(t.stop)

    def status(self, signature, timestamp=None, body=BODY):
        try:
            ingest._verify(SECRET, body, signature, timestamp)
        except HTTPException as e:
            return e.status_code
        return 200

    def test_a_fresh_timestamped_request_passes(self):
        ts = str(NOW)
        self.assertEqual(self.status(sign_at(ts), ts), 200)

    def test_the_window_is_five_minutes_either_side(self):
        for offset, expected in ((0, 200), (299, 200), (300, 200), (301, 401), (-299, 200), (-301, 401), (3600, 401), (-86400, 401)):
            ts = str(NOW - offset)
            self.assertEqual(self.status(sign_at(ts), ts), expected, f"{offset}s old")

    def test_a_captured_request_cannot_be_replayed_once_it_is_old(self):
        captured_ts = str(NOW - 1000)
        captured_sig = sign_at(captured_ts)
        self.assertEqual(self.status(captured_sig, captured_ts), 401)

    def test_a_fresh_timestamp_cannot_be_attached_to_an_old_capture(self):
        old_sig = sign_at(str(NOW - 1000))
        self.assertEqual(self.status(old_sig, str(NOW)), 401)
        self.assertEqual(self.status(sign(), str(NOW)), 401, "a body-only signature does not pass as a timestamped one")

    def test_the_body_and_the_secret_still_matter(self):
        ts = str(NOW)
        self.assertEqual(self.status(sign_at(ts), ts, body=b"tampered"), 401)
        self.assertEqual(self.status(sign_at(ts, secret="another"), ts), 401)

    def test_a_strange_timestamp_is_a_401_never_a_crash(self):
        for junk in ("", "abc", "-5", "1.5", "1e9", " 1800000000", "0x10", "\u0661\u0662", "9" * 400, "\x00"):
            self.assertEqual(self.status(sign_at(junk), junk), 401, repr(junk))

    def test_old_clients_still_work_until_a_timestamp_is_required(self):
        self.assertEqual(self.status(sign()), 200)
        with patch.object(settings, "require_signed_timestamp", True):
            self.assertEqual(self.status(sign()), 401, "a body-only request is refused once timestamps are required")
            ts = str(NOW)
            self.assertEqual(self.status(sign_at(ts), ts), 200)

    def test_the_window_can_be_changed(self):
        ts = str(NOW - 100)
        with patch.object(settings, "signature_max_age_s", 60):
            self.assertEqual(self.status(sign_at(ts), ts), 401)


if __name__ == "__main__":
    unittest.main()
