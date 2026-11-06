"""Password hashing and one-time codes (hub/credentials.py). No database.

    python -m unittest tests.test_credentials -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import credentials as c  # noqa: E402

KEY = "k" * 40


class Passwords(unittest.TestCase):
    def test_a_password_verifies_and_a_wrong_one_does_not(self):
        stored = c.hash_password("correct horse battery")
        self.assertTrue(c.verify_password("correct horse battery", stored))
        self.assertFalse(c.verify_password("correct horse batterz", stored))

    def test_the_hash_is_salted_and_never_holds_the_password(self):
        a, b = c.hash_password("same password here"), c.hash_password("same password here")
        self.assertNotEqual(a, b)
        self.assertNotIn("same password", a)

    def test_garbage_in_the_database_is_a_failed_login_not_a_crash(self):
        for stored in ("", "plain", "scrypt$1$2", "bcrypt$a$b$c$d$e", "scrypt$x$y$z$!!$!!"):
            self.assertFalse(c.verify_password("anything", stored), stored)

    def test_the_cost_is_read_from_the_stored_hash(self):
        # a hash made at an older, cheaper cost still verifies after the defaults are raised
        import base64, hashlib

        salt = b"0123456789abcdef"
        digest = hashlib.scrypt(b"old password", salt=salt, n=2**14, r=8, p=1, dklen=32)
        stored = f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
        self.assertTrue(c.verify_password("old password", stored))

    def test_short_and_repetitive_passwords_are_refused(self):
        self.assertIsNotNone(c.password_problem("short"))
        self.assertIsNotNone(c.password_problem("aaaaaaaaaaaaaaaa"))
        self.assertIsNone(c.password_problem("a long enough passphrase"))


class Totp(unittest.TestCase):
    def test_rfc6238_vector(self):
        # RFC 6238 appendix B, SHA-1, secret "12345678901234567890", T=59 -> 94287082 (8 digits); last 6 are 287082
        self.assertEqual(c._code(b"12345678901234567890", 59 // 30), "287082")

    def test_the_secret_is_derived_per_operator_and_per_version_and_needs_the_key(self):
        base = c.totp_secret(KEY, 1, 1)
        self.assertEqual(base, c.totp_secret(KEY, 1, 1))
        self.assertNotEqual(base, c.totp_secret(KEY, 2, 1))
        self.assertNotEqual(base, c.totp_secret(KEY, 1, 2))
        self.assertNotEqual(base, c.totp_secret("j" * 40, 1, 1))
        self.assertEqual(len(base), 20)

    def test_the_current_code_is_accepted_and_returns_its_step(self):
        s, now = c.totp_secret(KEY, 1, 1), 1_700_000_000
        code = c._code(s, c.totp_step(now))
        self.assertEqual(c.totp_match(s, code, 0, now), c.totp_step(now))

    def test_one_step_of_clock_drift_is_tolerated_two_are_not(self):
        s, now = c.totp_secret(KEY, 1, 1), 1_700_000_000
        step = c.totp_step(now)
        self.assertIsNotNone(c.totp_match(s, c._code(s, step - 1), 0, now))
        self.assertIsNotNone(c.totp_match(s, c._code(s, step + 1), 0, now))
        self.assertIsNone(c.totp_match(s, c._code(s, step - 2), 0, now))
        self.assertIsNone(c.totp_match(s, c._code(s, step + 2), 0, now))

    def test_a_code_cannot_be_used_twice(self):
        s, now = c.totp_secret(KEY, 1, 1), 1_700_000_000
        code = c._code(s, c.totp_step(now))
        used = c.totp_match(s, code, 0, now)
        self.assertIsNone(c.totp_match(s, code, used, now), "replay of the same code")
        older = c._code(s, c.totp_step(now) - 1)
        self.assertIsNone(c.totp_match(s, older, used, now), "an older step than the last accepted one")

    def test_wrong_shape_is_refused_not_crashed_on(self):
        s = c.totp_secret(KEY, 1, 1)
        for bad in ("", "12345", "1234567", "abcdef", "12 34 5"):
            self.assertIsNone(c.totp_match(s, bad, 0), repr(bad))

    def test_spaces_in_a_pasted_code_are_ignored(self):
        s, now = c.totp_secret(KEY, 1, 1), 1_700_000_000
        code = c._code(s, c.totp_step(now))
        self.assertIsNotNone(c.totp_match(s, f"{code[:3]} {code[3:]}", 0, now))

    def test_the_provisioning_uri_carries_the_secret_and_names_the_account(self):
        s = c.totp_secret(KEY, 1, 1)
        uri = c.totp_uri(s, "brandon")
        self.assertTrue(uri.startswith("otpauth://totp/Nightjar%3Abrandon?secret="))
        self.assertIn(c.totp_base32(s), uri)


class Recovery(unittest.TestCase):
    def test_codes_are_distinct_and_only_their_hashes_are_kept(self):
        codes = c.new_recovery_codes()
        self.assertEqual(len(set(codes)), len(codes))
        hashes = [c.hash_recovery(x) for x in codes]
        for code in codes:
            self.assertNotIn(code, hashes)

    def test_a_code_works_once_and_is_spent(self):
        codes = c.new_recovery_codes(3)
        hashes = [c.hash_recovery(x) for x in codes]
        left = c.use_recovery(codes[1], hashes)
        self.assertEqual(len(left), 2)
        self.assertIsNone(c.use_recovery(codes[1], left), "the same code a second time")
        self.assertIsNotNone(c.use_recovery(codes[0], left))

    def test_case_dashes_and_spaces_do_not_matter_when_typing_it(self):
        code = c.new_recovery_codes(1)[0]
        hashes = [c.hash_recovery(code)]
        typed = f"{code[:5].upper()}-{code[5:]} "
        self.assertEqual(c.use_recovery(typed, hashes), [])

    def test_an_unknown_code_is_refused(self):
        self.assertIsNone(c.use_recovery("nope", [c.hash_recovery("abcdefghjk")]))


class SessionTokens(unittest.TestCase):
    def test_the_database_value_is_the_hash_of_the_cookie_value(self):
        token, stored = c.new_session_token()
        self.assertEqual(stored, c.hash_token(token))
        self.assertNotEqual(token, stored)
        self.assertNotEqual(c.new_session_token()[0], token)
        self.assertGreaterEqual(len(token), 43)


if __name__ == "__main__":
    unittest.main()
