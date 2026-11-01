"""Derived site secrets: stable, separate per site and per version, and never the empty string.

    python -m unittest tests.test_derive -v

No database: the functions take plain objects.
"""

import hashlib
import hmac
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from hub import derive  # noqa: E402
from hub.config import settings  # noqa: E402

MASTER = "m" * 40


def site(id=1, version=1, stored="legacy-secret"):
    return SimpleNamespace(id=id, secret_version=version, ingest_secret=stored)


class Derive(unittest.TestCase):
    def test_it_is_stable(self):
        self.assertEqual(derive.derive(MASTER, 7, 1), derive.derive(MASTER, 7, 1))

    def test_it_differs_per_site_per_version_and_per_master_key(self):
        base = derive.derive(MASTER, 1, 1)
        self.assertNotEqual(base, derive.derive(MASTER, 2, 1))
        self.assertNotEqual(base, derive.derive(MASTER, 1, 2))
        self.assertNotEqual(base, derive.derive("n" * 40, 1, 1))

    def test_ids_cannot_be_confused_across_the_separator(self):
        # site 1 version 11 must not equal site 11 version 1
        self.assertNotEqual(derive.derive(MASTER, 1, 11), derive.derive(MASTER, 11, 1))

    def test_it_is_a_fixed_known_value(self):
        # Pins the derivation. If this fails, every derived site's secret has changed: do not "fix" the expected value.
        expected = hmac.new(MASTER.encode(), b"nightjar-site-secret-v1|1|1", hashlib.sha256).digest()
        import base64
        self.assertEqual(derive.derive(MASTER, 1, 1), base64.urlsafe_b64encode(expected).rstrip(b"=").decode())

    def test_it_fits_the_column_and_is_url_safe(self):
        s = derive.derive(MASTER, 1, 1)
        self.assertEqual(len(s), 43)
        self.assertRegex(s, r"^[A-Za-z0-9_-]+$")


class SiteSecret(unittest.TestCase):
    def test_a_legacy_site_uses_its_stored_secret_even_with_a_master_key(self):
        with patch.object(settings, "secret_master_key", MASTER):
            self.assertEqual(derive.site_secret(site(version=0)), "legacy-secret")

    def test_a_missing_version_counts_as_legacy(self):
        self.assertEqual(derive.site_secret(site(version=None)), "legacy-secret")

    def test_a_derived_site_uses_the_derived_secret_and_ignores_the_stored_one(self):
        with patch.object(settings, "secret_master_key", MASTER):
            self.assertEqual(derive.site_secret(site(id=3, version=2, stored="")), derive.derive(MASTER, 3, 2))

    def test_a_derived_site_without_a_master_key_fails_loudly_and_never_signs_with_nothing(self):
        with patch.object(settings, "secret_master_key", ""):
            with self.assertRaises(HTTPException) as ctx:
                derive.site_secret(site(version=1, stored=""))
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
