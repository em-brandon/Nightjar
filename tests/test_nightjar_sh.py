"""client/nightjar.sh against a stub hub: what it sends, how it signs, and that it never breaks the job it runs in.

The signature is checked with the hub's own `_verify`, so the script and the hub cannot drift apart.
No database needed.

    python -m unittest tests.test_nightjar_sh -v
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

from hub import ingest  # noqa: E402
from hub.config import settings  # noqa: E402
from unittest.mock import patch  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client", "nightjar.sh")
SH = shutil.which("sh")
SECRET = "s3cret-for-the-test"


class Stub(BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Stub.seen.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(Stub.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@unittest.skipUnless(SH and shutil.which("curl") and shutil.which("openssl"), "needs sh, curl and openssl")
class Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Stub)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Stub.seen = []
        Stub.status = 200

    def run_script(self, *args, secret=SECRET, url=None, strict=False, key="KEY123"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NIGHTJAR_")}
        env["NIGHTJAR_URL"] = url if url is not None else f"http://127.0.0.1:{self.port}"
        env["NIGHTJAR_KEY"] = key
        if secret:
            env["NIGHTJAR_SECRET"] = secret
        if strict:
            env["NIGHTJAR_STRICT"] = "1"
        return subprocess.run([SH, SCRIPT, *args], env=env, capture_output=True, text=True, timeout=30)

    def sent(self):
        self.assertEqual(len(Stub.seen), 1, "exactly one request")
        return Stub.seen[0]

    # --- heartbeat -----------------------------------------------------------------------------------------

    def test_a_bare_heartbeat_posts_an_empty_object_to_the_right_path(self):
        r = self.run_script("heartbeat", "nightly-backup")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        req = self.sent()
        self.assertEqual(req["path"], "/ingest/KEY123/heartbeat/nightly-backup")
        self.assertEqual(json.loads(req["body"]), {})

    def test_every_grace_kind_are_sent_in_seconds(self):
        self.run_script("heartbeat", "j", "--every", "1d", "--grace", "90s", "--kind", "app")
        self.assertEqual(json.loads(self.sent()["body"]), {"interval_s": 86400, "grace_s": 90, "kind": "app"})

    def test_durations_in_every_unit(self):
        for text, want in [("45", 45), ("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400)]:
            Stub.seen = []
            self.run_script("heartbeat", "j", "--every", text)
            self.assertEqual(json.loads(self.sent()["body"]), {"interval_s": want}, text)

    def test_fail_sends_the_message_with_awkward_characters_intact(self):
        message = 'exit 2: "disk full"\nline two\\with backslash\ttab'
        self.run_script("heartbeat", "j", "--fail", message)
        self.assertEqual(json.loads(self.sent()["body"]), {"status": "fail", "message": message})

    def test_the_signature_is_the_one_the_hub_accepts(self):
        self.run_script("heartbeat", "j", "--every", "5m")
        req = self.sent()
        h = {k.lower(): v for k, v in req["headers"].items()}
        with patch.object(settings, "verify_signatures", True):
            ingest._verify(SECRET, req["body"], h["x-hub-signature"], h["x-hub-timestamp"])  # raises if wrong
            with self.assertRaises(HTTPException):
                ingest._verify("another-secret", req["body"], h["x-hub-signature"], h["x-hub-timestamp"])

    def test_the_secret_is_never_sent(self):
        self.run_script("heartbeat", "j")
        req = self.sent()
        self.assertNotIn(SECRET.encode(), req["body"])
        self.assertNotIn(SECRET, json.dumps(req["headers"]))

    def test_without_a_secret_it_sends_unsigned(self):
        self.run_script("heartbeat", "j", secret="")
        h = {k.lower() for k in self.sent()["headers"]}
        self.assertNotIn("x-hub-signature", h)

    # --- deploy --------------------------------------------------------------------------------------------

    def test_deploy_sends_the_ref_status_and_progress(self):
        self.run_script("deploy", "main@1f9c4a2", "running", "--progress", "40")
        req = self.sent()
        self.assertEqual(req["path"], "/ingest/KEY123/deploy")
        self.assertEqual(json.loads(req["body"]), {"ref": "main@1f9c4a2", "status": "running", "progress": 40})

    def test_deploy_refuses_a_status_the_hub_would_not_know(self):
        r = self.run_script("deploy", "main@1", "great", strict=True)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(Stub.seen, [])

    # --- it must never break the job -----------------------------------------------------------------------

    def test_a_hub_that_is_down_is_a_warning_and_exit_zero(self):
        r = self.run_script("heartbeat", "j", url="http://127.0.0.1:1")
        self.assertEqual(r.returncode, 0, "a Nightjar outage must not fail the backup it follows")
        self.assertIn("could not reach the hub", r.stderr)

    def test_a_refused_signature_is_explained_and_still_exit_zero(self):
        Stub.status = 401
        r = self.run_script("heartbeat", "j")
        self.assertEqual(r.returncode, 0)
        self.assertIn("401", r.stderr)

    def test_strict_mode_turns_the_same_failures_into_exit_one(self):
        self.assertEqual(self.run_script("heartbeat", "j", url="http://127.0.0.1:1", strict=True).returncode, 1)
        Stub.status = 404
        self.assertEqual(self.run_script("heartbeat", "j", strict=True).returncode, 1)

    def test_a_missing_option_value_is_a_warning_not_a_shell_crash(self):
        r = self.run_script("heartbeat", "j", "--every")
        self.assertEqual(r.returncode, 0)
        self.assertIn("needs a value", r.stderr)
        self.assertEqual(Stub.seen, [])

    def test_a_bad_duration_sends_nothing(self):
        r = self.run_script("heartbeat", "j", "--every", "soon")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--every", r.stderr)
        self.assertEqual(Stub.seen, [])

    def test_plain_http_to_a_real_host_is_refused_so_a_secret_never_crosses_the_wire_in_clear(self):
        r = self.run_script("heartbeat", "j", url="http://nightjar.example.co.ke", strict=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("https", r.stderr)

    def test_no_arguments_prints_usage_and_exits_zero(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertIn("heartbeat", r.stderr)


if __name__ == "__main__":
    unittest.main()
