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


if __name__ == "__main__":
    unittest.main()
