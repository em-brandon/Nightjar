"""The Python host reporters must not follow a redirect with a signed report (security audit, low-severity item).

    python -m unittest tests.test_reporter_redirects -v

A report carries signature headers. If the hub address ever answered with a redirect (a misconfigured proxy, a hijacked name),
urllib would send those headers on to wherever it pointed. Real local servers: one that redirects and one that must never be
reached.
"""

import os
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client")
sys.path.insert(0, CLIENT)
# thermal_probe reads its settings when it is imported (the same two lines as tests/test_signing_contract.py)
os.environ.setdefault("NIGHTJAR_INGEST_URL", "https://nightjar.example/ingest/K/event")
os.environ.setdefault("NIGHTJAR_INGEST_SECRET", "s3cret")

import home_reporter  # noqa: E402
import jellyfin_reporter  # noqa: E402
import stack_reporter  # noqa: E402
import thermal_probe  # noqa: E402


class Server:
    def __init__(self, handler):
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class NoRedirects(unittest.TestCase):
    def setUp(self):
        self.reached = []
        reached = self.reached

        class Target(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                reached.append(dict(self.headers))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *a):
                pass

        self.target = Server(Target)
        where = f"http://127.0.0.1:{self.target.port}/elsewhere"

        class Redirecting(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)  # urllib follows this one, turning the POST into a GET but keeping the headers
                self.send_header("Location", where)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.hub = Server(Redirecting)
        self.addCleanup(self.hub.close)
        self.addCleanup(self.target.close)

    def test_a_redirect_is_an_error_and_the_signature_goes_nowhere_else(self):
        base = f"http://127.0.0.1:{self.hub.port}"
        for module in (stack_reporter, home_reporter, jellyfin_reporter):
            with self.assertRaises(urllib.error.HTTPError, msg=module.__name__) as cm:
                module.post(base, "KEY", "secret", {"x": 1})
            self.assertEqual(cm.exception.code, 302)
        self.assertEqual(self.reached, [], "the redirect target was never contacted")

    def test_the_thermal_probe_does_not_follow_one_either(self):
        from unittest.mock import patch

        with patch.object(thermal_probe, "INGEST_URL", f"http://127.0.0.1:{self.hub.port}/ingest/K/event"):
            with self.assertRaises(urllib.error.HTTPError):
                thermal_probe.report("ThermalWarning", "CPU running hot", "error", {})
        self.assertEqual(self.reached, [])

    def test_an_ordinary_answer_still_works(self):
        for module in (stack_reporter, home_reporter, jellyfin_reporter):
            module.post(f"http://127.0.0.1:{self.target.port}", "KEY", "secret", {"x": 1})
        self.assertEqual(len(self.reached), 3)
        self.assertTrue(all("X-Hub-Signature" in h for h in self.reached))


if __name__ == "__main__":
    unittest.main()
