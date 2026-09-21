"""Tests for the stack reporter's judgement calls.

Run from the repo root:  python -m unittest discover -s client -p "test_*.py" -v

The important ones are about telling a *stop* from a *crash*. A false "degraded"
on every Docker Desktop restart trains you to ignore the page, and a missed
crash defeats the point of having it.
"""

import unittest

import stack_reporter as r

NOW = 1_800_000_000.0  # any fixed instant


def ago(seconds):
    return NOW - seconds


def exited(code, finished_ago, oom=False):
    container = {"State": "exited", "Status": f"Exited ({code}) somewhere"}
    info = {"exit_code": code, "oom": oom, "finished_at": ago(finished_ago)}
    return container, info


class ServiceState(unittest.TestCase):
    def state(self, container, info=None):
        return r.service_state(container, info, NOW)

    # ---- things that are a STOP, not a failure -------------------------------

    def test_clean_exit_is_stopped_with_no_note(self):
        self.assertEqual(self.state(*exited(0, 3600)), ("stopped", None))

    def test_docker_desktop_and_host_stops_are_not_crashes(self):
        # 137 SIGKILL, 143 SIGTERM, 255 daemon/VM stop: all just moments old,
        # which is exactly when a naive rule would raise an alarm.
        for code in (137, 143, 255):
            with self.subTest(code=code):
                state, note = self.state(*exited(code, 30))
                self.assertEqual(state, "stopped")
                self.assertIn(f"exited {code}", note)

    # ---- real crashes must still be caught -----------------------------------

    def test_fresh_crash_is_unhealthy(self):
        for code in (1, 2, 134, 139):
            with self.subTest(code=code):
                state, note = self.state(*exited(code, 120))
                self.assertEqual(state, "unhealthy")
                self.assertIn(f"crashed with exit code {code}", note)

    def test_old_crash_is_just_down_but_the_note_keeps_the_facts(self):
        state, note = self.state(*exited(1, 3 * 86400))
        self.assertEqual(state, "stopped")
        self.assertIn("crashed with exit code 1", note)
        self.assertIn("3 days ago", note)

    def test_crash_window_boundary(self):
        just_in = r.CRASH_WINDOW - 1
        just_out = r.CRASH_WINDOW + 1
        self.assertEqual(self.state(*exited(1, just_in))[0], "unhealthy")
        self.assertEqual(self.state(*exited(1, just_out))[0], "stopped")

    def test_out_of_memory_kill_is_a_failure_even_with_the_kill_exit_code(self):
        # 137 alone is a stop, but 137 with OOMKilled is the kernel killing it for
        # using too much memory. That is the failure the memory limit exists for.
        state, note = self.state(*exited(137, 60, oom=True))
        self.assertEqual(state, "unhealthy")
        self.assertIn("out of memory", note)

    def test_old_oom_kill_is_stopped_but_still_noted(self):
        state, note = self.state(*exited(137, 5 * 86400, oom=True))
        self.assertEqual(state, "stopped")
        self.assertIn("out of memory", note)

    # ---- running and other states --------------------------------------------

    def test_running(self):
        self.assertEqual(self.state({"State": "running", "Status": "Up 2 hours (healthy)"}), ("running", None))
        self.assertEqual(self.state({"State": "running", "Status": "Up 2 hours"}), ("running", None))

    def test_failing_health_check_is_unhealthy(self):
        state, note = self.state({"State": "running", "Status": "Up 2 hours (unhealthy)"})
        self.assertEqual(state, "unhealthy")
        self.assertIn("health check", note)

    def test_restarting_and_dead_are_unhealthy(self):
        self.assertEqual(self.state({"State": "restarting", "Status": "Restarting (1) 5 seconds ago"})[0], "unhealthy")
        self.assertEqual(self.state({"State": "dead", "Status": "Dead"})[0], "unhealthy")

    def test_created_and_paused_are_stopped(self):
        for st in ("created", "paused"):
            self.assertEqual(self.state({"State": st, "Status": st})[0], "stopped")

    def test_without_inspect_data_it_falls_back_to_the_status_text(self):
        # docker inspect can fail; a crash code must still be seen, and a stop must not
        self.assertEqual(r.service_state({"State": "exited", "Status": "Exited (255) 3 weeks ago"}, None, NOW)[0], "stopped")
        # no finish time means it cannot be called fresh, so an unknown crash reads as down
        self.assertEqual(r.service_state({"State": "exited", "Status": "Exited (1) 3 weeks ago"}, None, NOW)[0], "stopped")


class StackState(unittest.TestCase):
    def test_states(self):
        self.assertEqual(r.stack_state([]), "stopped")
        self.assertEqual(r.stack_state(["running", "running"]), "running")
        self.assertEqual(r.stack_state(["stopped", "stopped"]), "stopped")
        self.assertEqual(r.stack_state(["running", "stopped"]), "degraded")
        self.assertEqual(r.stack_state(["running", "unhealthy"]), "degraded")
        self.assertEqual(r.stack_state(["stopped", "unhealthy"]), "degraded")

    def test_a_whole_stack_stopped_by_docker_is_stopped_not_degraded(self):
        states = [r.service_state(*exited(255, 20)[:1], exited(255, 20)[1], NOW)[0] for _ in range(3)]
        self.assertEqual(r.stack_state(states), "stopped")


class Parsing(unittest.TestCase):
    def test_docker_timestamps(self):
        # nine fractional digits and a trailing Z, as docker prints them
        self.assertIsNotNone(r.parse_time("2026-08-27T04:28:20.596187871Z"))
        self.assertIsNotNone(r.parse_time("2026-08-27T04:28:20Z"))
        # never finished
        self.assertIsNone(r.parse_time("0001-01-01T00:00:00Z"))
        self.assertIsNone(r.parse_time(""))
        self.assertIsNone(r.parse_time("garbage"))

    def test_sizes(self):
        self.assertEqual(r.parse_size("1.5GiB"), 1.5 * 1024**3)
        self.assertEqual(r.parse_size("512kB"), 512_000)
        self.assertEqual(r.parse_size("0B"), 0)
        self.assertEqual(r.parse_size(""), 0)
        self.assertEqual(r.parse_size("N/A"), 0)

    def test_labels(self):
        self.assertEqual(r.parse_labels("a=1,com.docker.compose.project=x")["com.docker.compose.project"], "x")

    def test_humanize(self):
        self.assertEqual(r.humanize(5), "just now")
        self.assertEqual(r.humanize(120), "2 minutes ago")
        self.assertEqual(r.humanize(3600), "1 hour ago")
        self.assertEqual(r.humanize(3 * 86400), "3 days ago")


if __name__ == "__main__":
    unittest.main()
