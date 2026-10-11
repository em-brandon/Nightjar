"""The alert policy: the routing matrix, quiet hours, and the rule that nothing is suppressed.
Pure functions, no database.

    python -m unittest tests.test_policy -v
"""

import os
import sys
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import policy  # noqa: E402
from hub.policy import EMERGENCY, NORMAL, QUIET, decide, decide_safely, is_quiet  # noqa: E402

# Nairobi is UTC+3 all year, so these are local 12:00 and 23:00.
DAY = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
NIGHT = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)


class QuietHours(unittest.TestCase):
    def local(self, hour, minute=0):
        # an instant that is the given local Nairobi time
        return datetime(2026, 10, 12, (hour - 3) % 24, minute, tzinfo=UTC)

    def test_the_window_wraps_midnight_and_includes_its_start_but_not_its_end(self):
        self.assertTrue(is_quiet(self.local(22, 0)))
        self.assertTrue(is_quiet(self.local(23, 59)))
        self.assertTrue(is_quiet(self.local(3)))
        self.assertTrue(is_quiet(self.local(6, 59)))
        self.assertFalse(is_quiet(self.local(7, 0)))
        self.assertFalse(is_quiet(self.local(12)))
        self.assertFalse(is_quiet(self.local(21, 59)))

    def test_it_uses_the_local_zone_not_utc(self):
        # 19:30 UTC is 22:30 in Nairobi
        self.assertTrue(is_quiet(datetime(2026, 10, 12, 19, 30, tzinfo=UTC)))
        # 17:00 UTC is 20:00 in Nairobi
        self.assertFalse(is_quiet(datetime(2026, 10, 12, 17, 0, tzinfo=UTC)))

    def test_an_empty_window_is_never_quiet(self):
        self.assertFalse(is_quiet(self.local(3), start=5, end=5))

    def test_a_window_inside_one_day_works_too(self):
        self.assertTrue(is_quiet(self.local(13), start=12, end=14))
        self.assertFalse(is_quiet(self.local(14), start=12, end=14))


class Matrix(unittest.TestCase):
    """CLAUDE.md's routing matrix, cell by cell."""

    def test_p1_always_wakes_the_phone_at_priority_two(self):
        for presence in ("home", "away"):
            for now in (DAY, NIGHT):
                d = decide("P1", "new", presence, now)
                self.assertTrue(d.push, (presence, now))
                self.assertEqual(d.priority, EMERGENCY)
                self.assertFalse(d.digest_only)

    def test_p1_adds_the_speaker_only_when_home(self):
        self.assertEqual(decide("P1", "new", "home", DAY).speaker, "alarm")
        self.assertEqual(decide("P1", "new", "home", NIGHT).speaker, "alarm")
        self.assertIsNone(decide("P1", "new", "away", DAY).speaker)
        self.assertIsNone(decide("P1", "new", "away", NIGHT).speaker)

    def test_p2_by_day_is_a_normal_push_with_a_chime_when_home(self):
        home = decide("P2", "new", "home", DAY)
        away = decide("P2", "new", "away", DAY)
        self.assertEqual((home.push, home.priority, home.speaker), (True, NORMAL, "chime"))
        self.assertEqual((away.push, away.priority, away.speaker), (True, NORMAL, None))

    def test_p2_at_night_is_delivered_without_sound_whoever_is_home(self):
        for presence in ("home", "away"):
            d = decide("P2", "new", presence, NIGHT)
            self.assertEqual((d.push, d.priority, d.speaker), (True, QUIET, None), presence)

    def test_p3_goes_to_the_digest_at_any_time(self):
        for presence in ("home", "away"):
            for now in (DAY, NIGHT):
                d = decide("P3", "new", presence, now)
                self.assertEqual((d.push, d.digest_only), (False, True))

    def test_every_alerting_change_counts(self):
        for change in ("new", "regression", "spike"):
            self.assertTrue(decide("P2", change, "away", DAY).push, change)

    def test_a_known_open_issue_sends_nothing(self):
        d = decide("P1", None, "away", DAY)
        self.assertEqual((d.push, d.digest_only), (False, False))


class NothingIsSuppressed(unittest.TestCase):
    def test_no_p1_or_p2_is_ever_left_unsent(self):
        # exhaustive: every severity that can notify, every presence value, every hour of the day
        for severity in ("P1", "P2"):
            for presence in ("home", "away", "", "nonsense", None):
                for hour in range(24):
                    now = datetime(2026, 10, 12, hour, 0, tzinfo=UTC)
                    for change in ("new", "regression", "spike"):
                        d = decide(severity, change, presence, now)
                        self.assertTrue(d.push, (severity, presence, hour, change))

    def test_unknown_presence_counts_as_away(self):
        self.assertIsNone(decide("P1", "new", "nonsense", DAY).speaker)
        self.assertIsNone(decide("P2", "new", None, DAY).speaker)

    def test_an_unknown_severity_is_still_sent(self):
        d = decide("P9", "new", "away", DAY)
        self.assertTrue(d.push)
        self.assertEqual(d.priority, NORMAL)

    def test_a_policy_that_raises_falls_back_to_a_normal_push(self):
        with patch.object(policy, "is_quiet", side_effect=RuntimeError("boom")):
            d = decide_safely("P1", "new", "home", DAY)
        self.assertEqual((d.push, d.priority, d.digest_only), (True, NORMAL, False))
        self.assertIn("policy error", d.reason)

    def test_a_working_policy_is_not_second_guessed(self):
        self.assertEqual(decide_safely("P1", "new", "away", DAY), decide("P1", "new", "away", DAY))


if __name__ == "__main__":
    unittest.main()
