"""The rules that decide when a staging stack's problem opens an issue and when it
resolves itself. Pure functions, no database, so every edge can be covered.

    python -m unittest discover -s tests -v

The auto-resolve rule is deliberately strict. A resolved issue that relapses is a
regression (an alert, once chunk 2 exists), so a flapping stack must not resolve
and reopen all day, and a reporter that went quiet must never count as healthy.
"""

import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import stacks  # noqa: E402
from hub.config import settings  # noqa: E402

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
NONE: set[str] = set()


def timeline(seconds_ago, bad=None):
    """Oldest first. `bad` maps seconds-ago -> the set of problems that report showed."""
    bad = bad or {}
    return [(NOW - timedelta(seconds=s), bad.get(s, NONE)) for s in sorted(seconds_ago, reverse=True)]


def every_minute(minutes, bad=None):
    return timeline(range(0, minutes * 60 + 1, 60), bad)


class HealthyFor(unittest.TestCase):
    def setUp(self):
        self.window = 600  # the default 10 minutes, in seconds
        p = patch.object(settings, "stack_healthy_min", 10)
        p.start()
        self.addCleanup(p.stop)

    def healthy(self, tl, key="degraded"):
        return stacks._healthy_for(key, tl, NOW)

    # ---- resolves ------------------------------------------------------------

    def test_ten_minutes_of_clean_reports_resolves(self):
        self.assertTrue(self.healthy(every_minute(11)))

    def test_a_problem_that_ended_before_the_window_does_not_count_against_it(self):
        # bad 11.5 minutes ago, clean ever since: it has been gone a full window
        tl = timeline(range(0, 691, 60), {690: {"degraded"}})
        self.assertTrue(self.healthy(tl))

    def test_other_problems_do_not_block_this_one(self):
        # memory is still high, but the stack itself recovered: resolve "degraded" only
        tl = every_minute(11, {s: {"memory_high"} for s in range(0, 661, 60)})
        self.assertTrue(self.healthy(tl, "degraded"))
        self.assertFalse(self.healthy(tl, "memory_high"))

    # ---- refuses -------------------------------------------------------------

    def test_a_problem_inside_the_window_blocks(self):
        # it recovered four minutes ago: a flapping stack must not resolve
        self.assertFalse(self.healthy(every_minute(11, {240: {"degraded"}})))

    def test_the_report_being_processed_counts(self):
        self.assertFalse(self.healthy(every_minute(11, {0: {"degraded"}})))

    def test_not_enough_history_cannot_vouch_for_the_window(self):
        self.assertFalse(self.healthy(every_minute(5)))

    def test_empty_and_single_point_timelines(self):
        self.assertFalse(self.healthy([]))
        self.assertFalse(self.healthy(timeline([0])))

    def test_history_boundary(self):
        # the earliest report may fall 90s short of the window, and no further
        self.assertTrue(self.healthy(timeline([510, 450, 390, 330, 270, 210, 150, 90, 30, 0])))
        self.assertFalse(self.healthy(timeline([509, 450, 390, 330, 270, 210, 150, 90, 30, 0])))

    def test_a_silent_reporter_is_not_evidence_of_health(self):
        # reports, then nothing for four minutes, then reports again. Whatever the
        # reports either side said, nobody was watching in between.
        tl = timeline([660, 600, 540, 300, 240, 180, 120, 60, 0])
        self.assertFalse(self.healthy(tl))

    def test_a_gap_right_at_the_start_of_the_window_is_caught(self):
        # last report before the window was 15 minutes ago, then nothing until 9m50s
        tl = timeline([900, 590, 530, 470, 410, 350, 290, 230, 170, 110, 50, 0])
        self.assertFalse(self.healthy(tl))

    def test_the_stale_threshold_is_the_largest_gap_allowed(self):
        gap = int(stacks.STALE_AFTER.total_seconds())
        ok = timeline([660, 660 - gap, 660 - 2 * gap, 660 - 3 * gap, 660 - 4 * gap] + list(range(0, 660 - 4 * gap + 1, 60)))
        # every gap here is exactly the threshold, which is allowed
        self.assertTrue(self.healthy(ok))
        too_far = timeline([660, 660 - gap - 1] + list(range(0, 660 - gap - 1, 60)))
        self.assertFalse(self.healthy(too_far))

    def test_the_window_is_configurable(self):
        with patch.object(settings, "stack_healthy_min", 2):
            self.assertTrue(self.healthy(every_minute(3)))
            self.assertFalse(self.healthy(every_minute(1)))


class Conditions(unittest.TestCase):
    def cond(self, state="running", mem=100, limit=1000, free=50_000, total=100_000, delta=0):
        return stacks._conditions(state, mem, limit, free, total, delta)

    def test_healthy_has_none(self):
        self.assertEqual(self.cond(), set())

    def test_degraded(self):
        self.assertEqual(self.cond(state="degraded"), {"degraded"})

    def test_stopped_is_not_a_problem(self):
        self.assertEqual(self.cond(state="stopped"), set())

    def test_restart_loop_threshold(self):
        self.assertEqual(self.cond(delta=stacks.RESTART_LOOP_MIN - 1), set())
        self.assertEqual(self.cond(delta=stacks.RESTART_LOOP_MIN), {"restart_loop"})

    def test_memory_boundary_is_85_percent_of_the_limit(self):
        self.assertEqual(self.cond(mem=849, limit=1000), set())
        self.assertEqual(self.cond(mem=850, limit=1000), {"memory_high"})

    def test_memory_needs_a_limit_to_be_near(self):
        self.assertEqual(self.cond(mem=10**9, limit=None), set())
        self.assertEqual(self.cond(mem=None, limit=1000), set())

    def test_disk_boundary_is_10_percent_free(self):
        self.assertEqual(self.cond(free=10_000, total=100_000), set())
        self.assertEqual(self.cond(free=9_999, total=100_000), {"disk_low"})

    def test_disk_unknown_is_not_low(self):
        self.assertEqual(self.cond(free=None, total=100_000), set())
        self.assertEqual(self.cond(free=5, total=None), set())

    def test_several_at_once(self):
        self.assertEqual(self.cond(state="degraded", mem=900, free=1, delta=5), {"degraded", "memory_high", "disk_low", "restart_loop"})


class RestartDelta(unittest.TestCase):
    def row(self, seconds_ago, restarts):
        return SimpleNamespace(reported_at=NOW - timedelta(seconds=seconds_ago), restarts=restarts)

    def test_delta_against_the_earliest_report_in_the_window(self):
        rows = [self.row(540, 1), self.row(300, 2)]
        self.assertEqual(stacks._restart_delta(rows, NOW, 6), 5)

    def test_reports_older_than_the_window_are_ignored(self):
        rows = [self.row(3600, 0), self.row(300, 4)]
        self.assertEqual(stacks._restart_delta(rows, NOW, 6), 2)

    def test_nothing_to_compare_against_is_zero_not_a_guess(self):
        self.assertEqual(stacks._restart_delta([], NOW, 9), 0)
        self.assertEqual(stacks._restart_delta([self.row(3600, 0)], NOW, 9), 0)

    def test_a_recreated_container_resets_its_counter(self):
        self.assertEqual(stacks._restart_delta([self.row(300, 8)], NOW, 0), 0)

    def test_unreported_counts_are_zero(self):
        self.assertEqual(stacks._restart_delta([self.row(300, 3)], NOW, None), 0)
        self.assertEqual(stacks._restart_delta([self.row(300, None)], NOW, 3), 0)


if __name__ == "__main__":
    unittest.main()
