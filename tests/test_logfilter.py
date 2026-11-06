"""A secret in a URL path must not reach the logs (hub/logfilter.py, deploy/Caddyfile).

    python -m unittest tests.test_logfilter -v
"""

import io
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import logfilter  # noqa: E402

TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678abcdef01"
CADDYFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "Caddyfile")


class Uvicorn(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.log = logging.getLogger("uvicorn.access")
        self.handler = logging.StreamHandler(self.stream)
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)
        for f in [f for f in self.log.filters if isinstance(f, logfilter.RedactSecretPaths)]:
            self.log.removeFilter(f)
        self.addCleanup(self.log.removeHandler, self.handler)
        self.addCleanup(lambda: [self.log.removeFilter(f) for f in list(self.log.filters) if isinstance(f, logfilter.RedactSecretPaths)])

    def line(self, path):
        # the shape uvicorn uses
        self.log.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5555", "POST", path, "1.1", 200)
        return self.stream.getvalue()

    def test_without_the_filter_the_secret_would_be_logged(self):
        self.assertIn(TOKEN, self.line(f"/pushover/ack/{TOKEN}"))

    def test_with_it_the_secret_is_gone_and_the_rest_of_the_line_is_not(self):
        logfilter.install()
        out = self.line(f"/pushover/ack/{TOKEN}")
        self.assertNotIn(TOKEN, out)
        self.assertIn('POST /pushover/ack/REDACTED HTTP/1.1" 200', out)

    def test_other_paths_and_queries_are_left_alone(self):
        logfilter.install()
        out = self.line("/api/sites?x=1")
        self.assertIn("/api/sites?x=1", out)

    def test_installing_twice_adds_one_filter(self):
        logfilter.install()
        logfilter.install()
        self.assertEqual(sum(isinstance(f, logfilter.RedactSecretPaths) for f in self.log.filters), 1)

    def test_the_hub_installs_it_when_it_starts(self):
        with open(os.path.join(os.path.dirname(CADDYFILE), "..", "hub", "main.py"), encoding="utf-8") as f:
            self.assertIn("logfilter.install()", f.read())


class Caddy(unittest.TestCase):
    def test_the_caddy_access_log_redacts_the_callback_path(self):
        # Read as text: Caddy is not run here, so the syntax itself is checked by hand on a real server (docker compose logs caddy shows a bad filter at start)
        with open(CADDYFILE, encoding="utf-8") as f:
            text = f.read()
        log_block = text[text.index("\tlog {"):]
        self.assertIn("format filter", log_block)
        self.assertIn("request>uri regexp", log_block)
        self.assertIn("/pushover/ack/", log_block)
        self.assertIn("wrap json", log_block)


if __name__ == "__main__":
    unittest.main()
