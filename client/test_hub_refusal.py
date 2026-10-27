"""The home-side scripts refuse a plain http:// hub, out loud.

    python -m unittest discover -s client

The Python, Node, PHP, Java and browser reporters already did (client/test_reporter.py and
tests/test_server_reporters.py). These four run on the staging host and the ThinkCentre, and carry Docker state, host
health, what is being streamed from the home media server (personal data) and thermal alarms, so a mistyped
NIGHTJAR_URL must not send them readable across a network. Refusing must be loud: a quiet host looks like a healthy one.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# thermal_probe reads its settings when it is imported
os.environ.setdefault("NIGHTJAR_INGEST_URL", "https://nightjar.example/ingest/K/event")
os.environ.setdefault("NIGHTJAR_INGEST_SECRET", "s3cret")

import home_reporter  # noqa: E402
import jellyfin_reporter  # noqa: E402
import stack_reporter  # noqa: E402
import thermal_probe  # noqa: E402

MODULES = (home_reporter, jellyfin_reporter, stack_reporter, thermal_probe)


class Rule(unittest.TestCase):
    def test_https_and_http_to_this_machine_are_fine(self):
        for m in MODULES:
            for ok in (
                "https://nightjar.example",
                "http://localhost:8000",
                "http://127.0.0.1:9",
                "http://[::1]:8000",
                "http://app.localhost",
            ):
                self.assertIsNone(m.hub_refusal(ok), (m.__name__, ok))

    def test_plain_http_elsewhere_and_anything_odd_is_refused(self):
        for m in MODULES:
            for bad in (
                "http://nightjar.example",
                "http://10.0.0.5",
                "http://192.168.1.20:8000",
                "ftp://nightjar.example",
                "nightjar.example",
                "//nightjar.example",
                "",
            ):
                self.assertIsNotNone(m.hub_refusal(bad), (m.__name__, bad))

    def test_the_reason_names_the_host_but_never_the_whole_address(self):
        for m in MODULES:
            why = m.hub_refusal("http://user:pa55word@nightjar.example/ingest/SECRETKEY/event?token=abc")
            self.assertIn("http://nightjar.example", why)
            for leak in ("pa55word", "SECRETKEY", "token=abc", "user:"):
                self.assertNotIn(leak, why, m.__name__)


class Refusing(unittest.TestCase):
    """With a plain http hub each script says so, gathers nothing and sends nothing."""

    ENV = {
        "NIGHTJAR_URL": "http://nightjar.example",
        "NIGHTJAR_HOME_KEY": "K",
        "NIGHTJAR_MEDIA_KEY": "K",
        "NIGHTJAR_STACKS": "does-not-exist.json",
        "JELLYFIN_API_KEY": "x",
    }

    def run_refused(self, module):
        with patch.dict(os.environ, self.ENV), patch("urllib.request.urlopen") as urlopen:
            err = io.StringIO()
            with redirect_stderr(err):
                module.safe_run()
        urlopen.assert_not_called()
        return err.getvalue()

    def test_each_script_prints_the_reason_and_sends_nothing(self):
        for module in (home_reporter, jellyfin_reporter, stack_reporter):
            said = self.run_refused(module)
            self.assertIn("refusing to report to http://nightjar.example", said, module.__name__)

    def test_it_refuses_before_doing_any_work(self):
        # the home reporter's speed test costs data; a refused hub must not spend it
        with patch.object(home_reporter, "speed_for_report") as speed:
            self.run_refused(home_reporter)
        speed.assert_not_called()

    def test_the_thermal_probe_exits_non_zero_so_a_timer_shows_failed(self):
        with patch.object(thermal_probe, "INGEST_URL", "http://nightjar.example/ingest/K/event"), patch(
            "urllib.request.urlopen"
        ) as urlopen:
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                thermal_probe.report("ThermalCritical", "CPU at critical temperature", "fatal", {})
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("refusing to report to http://nightjar.example", err.getvalue())
        urlopen.assert_not_called()

    def test_a_good_hub_still_sends(self):
        with patch.object(thermal_probe, "INGEST_URL", "https://nightjar.example/ingest/K/event"), patch(
            "urllib.request.urlopen"
        ) as urlopen:
            thermal_probe.report("ThermalWarning", "CPU running hot in enclosure", "error", {})
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
