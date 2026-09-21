"""Tests for the Jellyfin reporter's parsing.

Run from the repo root:  python -m unittest discover -s client -p "test_*.py" -v

The point is the difference between "Jellyfin is down" and "nobody is watching":
the first is a problem, the second is not, and a reporter that confused them would
either page for a quiet evening or call a dead server idle.
"""

import unittest

import jellyfin_reporter as r


def session(**over):
    base = {
        "Id": "abc",
        "UserName": "amani",
        "Client": "Jellyfin Roku",
        "DeviceName": "Living room TV",
        "NowPlayingItem": {
            "Id": "ep1",
            "Name": "The Pilot",
            "SeriesName": "Friends",
            "ParentIndexNumber": 1,
            "IndexNumber": 4,
            "Type": "Episode",
            "RunTimeTicks": 1320 * r.TICKS_PER_S,
        },
        "PlayState": {"PositionTicks": 90 * r.TICKS_PER_S, "IsPaused": False, "PlayMethod": "DirectPlay"},
    }
    return {**base, **over}


class Parse(unittest.TestCase):
    def test_an_episode_session(self):
        s = r.parse_session(session())
        self.assertEqual((s["user"], s["series"], s["episode"], s["kind"]), ("amani", "Friends", "S01E04", "Episode"))
        self.assertEqual((s["position_s"], s["runtime_s"], s["paused"]), (90, 1320, False))
        self.assertEqual(s["play_method"], "direct_play")

    def test_play_methods(self):
        for raw, want in [("DirectStream", "direct_stream"), ("Transcode", "transcode"), ("Weird", None)]:
            s = r.parse_session(session(PlayState={"PlayMethod": raw}))
            self.assertEqual(s["play_method"], want)

    def test_a_connected_but_idle_device_is_not_watching(self):
        self.assertIsNone(r.parse_session(session(NowPlayingItem=None)))
        self.assertIsNone(r.parse_session({"Id": "x", "UserName": "amani"}))

    def test_a_malformed_session_is_skipped_not_fatal(self):
        self.assertIsNone(r.parse_session(session(UserName=None)))
        self.assertIsNone(r.parse_session(session(NowPlayingItem={"Id": "x"})))
        # no season or number: a film or an oddly tagged file has no episode label
        movie = session()
        movie["NowPlayingItem"] = {"Id": "m", "Name": "A Film", "Type": "Movie"}
        self.assertIsNone(r.parse_session(movie)["episode"])

    def test_long_values_are_clipped_to_what_the_hub_accepts(self):
        s = r.parse_session(session(UserName="u" * 500))
        self.assertEqual(len(s["user"]), 120)

    def test_bad_tick_values_are_none(self):
        self.assertIsNone(r.ticks_to_s(None))
        self.assertIsNone(r.ticks_to_s(-5))
        self.assertIsNone(r.ticks_to_s("soon"))


class Report(unittest.TestCase):
    def test_down_is_not_the_same_as_idle(self):
        self.assertFalse(r.build_report(None, None, None)["up"])
        idle = r.build_report([], "10.10.3", None)
        self.assertTrue(idle["up"])
        self.assertEqual(idle["sessions"], [])

    def test_only_watching_sessions_are_reported(self):
        rep = r.build_report([session(), session(Id="idle", NowPlayingItem=None), "junk"], "10.10.3", None)
        self.assertEqual([s["session_id"] for s in rep["sessions"]], ["abc"])

    def test_disk_is_reported_in_mb_when_known(self):
        rep = r.build_report([], None, (1000 * r.MB, 250 * r.MB))
        self.assertEqual((rep["host_disk_total_mb"], rep["host_disk_free_mb"]), (1000, 250))
        self.assertNotIn("host_disk_total_mb", r.build_report([], None, None))

    def test_the_session_count_is_capped_to_what_the_hub_accepts(self):
        many = [session(Id=str(i)) for i in range(100)]
        self.assertEqual(len(r.build_report(many, None, None)["sessions"]), r.MAX_SESSIONS)


if __name__ == "__main__":
    unittest.main()
