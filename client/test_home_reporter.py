"""Tests for the home reporter's parsing and scheduling.

Run from the repo root:  python -m unittest discover -s client -p "test_*.py" -v

The judgement calls: a broken sensor must not be reported as a temperature, a speed test must not
run every minute (it costs data), a failed test must not erase the last good one, and a missing
backup path is "not tracked", which is different from "tracked and missing".
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

import home_reporter as r


class Meminfo(unittest.TestCase):
    def test_a_normal_file(self):
        text = "MemTotal:       24576000 kB\nMemFree:  100 kB\nMemAvailable:   15360000 kB\n"
        self.assertEqual(r.parse_meminfo(text), (24000, 15000))

    def test_missing_fields_are_none(self):
        self.assertIsNone(r.parse_meminfo("MemTotal: 100 kB\n"))
        self.assertIsNone(r.parse_meminfo(""))


class CpuTemp(unittest.TestCase):
    def test_the_hottest_sensor_wins(self):
        self.assertEqual(r.parse_cpu_temp(["45000\n", "61500\n", "38000"]), 61.5)

    def test_nonsense_sensors_are_ignored(self):
        self.assertEqual(r.parse_cpu_temp(["0", "-274000", "999999", "52000"]), 52.0)
        self.assertIsNone(r.parse_cpu_temp(["0", "junk", ""]))
        self.assertIsNone(r.parse_cpu_temp([]))


class Sensor(unittest.TestCase):
    def test_a_ds18b20_w1_slave_file(self):
        good = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"
        self.assertEqual(r.parse_sensor(good), 23.1)

    def test_a_failed_checksum_is_not_a_reading(self):
        bad = "72 01 4b 46 7f ff 0e 10 57 : crc=00 NO\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"
        self.assertIsNone(r.parse_sensor(bad))

    def test_thousandths_and_plain_decimals(self):
        self.assertEqual(r.parse_sensor("31562\n"), 31.6)
        self.assertEqual(r.parse_sensor("31.5"), 31.5)
        self.assertEqual(r.parse_sensor("-2000"), -2.0)

    def test_nonsense_is_none(self):
        self.assertIsNone(r.parse_sensor(""))
        self.assertIsNone(r.parse_sensor("hot"))
        self.assertIsNone(r.parse_sensor("t=-127000"))  # what a DS18B20 gives when unplugged
        self.assertIsNone(r.parse_sensor("200000"))

    def test_an_unset_or_missing_sensor_is_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIGHTJAR_INTAKE_SENSOR", None)
            self.assertIsNone(r.read_sensor("NIGHTJAR_INTAKE_SENSOR"))
        with patch.dict(os.environ, {"NIGHTJAR_INTAKE_SENSOR": "/no/such/file"}):
            self.assertIsNone(r.read_sensor("NIGHTJAR_INTAKE_SENSOR"))

    def test_a_configured_sensor_lands_in_the_host_stats(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "temperature")
            with open(path, "w") as fh:
                fh.write("29500\n")
            with patch.dict(os.environ, {"NIGHTJAR_INTAKE_SENSOR": path}):
                self.assertEqual(r.host_stats()["intake_c"], 29.5)


NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 5000000  100 0 0 0 0 0 0  5000000  100 0 0 0 0 0 0
  eth0: 1000000  800 0 0 0 0 0 0   400000  600 0 0 0 0 0 0
 wlan0:  500000  400 0 0 0 0 0 0   100000  300 0 0 0 0 0 0
docker0: 9000000  9 0 0 0 0 0 0  9000000  9 0 0 0 0 0 0
vethab: 7000000  9 0 0 0 0 0 0  7000000  9 0 0 0 0 0 0
tailscale0: 8000000 9 0 0 0 0 0 0 8000000 9 0 0 0 0 0 0
"""


class Traffic(unittest.TestCase):
    def test_only_the_real_interfaces_are_counted(self):
        # eth0 + wlan0; not loopback, Docker, veth pairs or Tailscale, whose bytes would be counted twice
        self.assertEqual(r.parse_net_dev(NET_DEV), (1_500_000, 500_000))

    def test_nothing_usable_is_none(self):
        self.assertIsNone(r.parse_net_dev(""))
        self.assertIsNone(r.parse_net_dev("h1\nh2\n    lo: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\n"))

    def test_the_rate_is_bits_per_second_over_the_gap(self):
        prev = {"at": 1000.0, "rx": 1_000_000, "tx": 200_000}
        # 750,000 bytes down in 60 s is 100,000 bit/s; 30,000 bytes up is 4,000 bit/s
        self.assertEqual(r.traffic_rates(prev, 1_750_000, 230_000, 1060.0), (100_000.0, 4_000.0))

    def test_no_fair_answer_is_none(self):
        prev = {"at": 1000.0, "rx": 1_000_000, "tx": 200_000}
        self.assertIsNone(r.traffic_rates(None, 5, 5, 1060.0), "the first run has nothing to compare with")
        self.assertIsNone(r.traffic_rates(prev, 2_000_000, 300_000, 1000.0 + 3600), "a long gap says nothing about now")
        self.assertIsNone(r.traffic_rates(prev, 2_000_000, 300_000, 900.0), "a clock that went backwards")
        self.assertIsNone(r.traffic_rates(prev, 10, 10, 1060.0), "counters that reset after a reboot")
        self.assertIsNone(r.traffic_rates({"at": "junk"}, 1, 1, 1060.0))

    def test_a_run_remembers_its_counters_for_the_next(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "home.json")
            with patch.object(r, "read_text", return_value=NET_DEV), patch.dict(os.environ, {"NIGHTJAR_SPEED": "0"}):
                _, first = r.speed_for_report(path, 1000.0)
                self.assertEqual(first, {}, "no previous counters yet")
                bumped = NET_DEV.replace("1000000  800", "1750000  800")
                with patch.object(r, "read_text", return_value=bumped):
                    _, second = r.speed_for_report(path, 1060.0)
            self.assertEqual(second["rx_bps"], 100_000.0)


class Speed(unittest.TestCase):
    def test_mbps(self):
        self.assertEqual(r.mbps(5_000_000, 4.0), 10.0)
        self.assertIsNone(r.mbps(5_000_000, 0))
        self.assertIsNone(r.mbps(0, 1))

    def test_a_test_is_due_only_after_the_interval(self):
        now = 1_000_000.0
        self.assertTrue(r.is_due({}, now, 60), "never tested")
        self.assertFalse(r.is_due({"attempted_at": now - 59 * 60}, now, 60))
        self.assertTrue(r.is_due({"attempted_at": now - 61 * 60}, now, 60))
        self.assertTrue(r.is_due({"attempted_at": now + 500}, now, 60), "a clock that went backwards")
        self.assertTrue(r.is_due({"attempted_at": "junk"}, now, 60))

    def test_the_test_runs_once_and_the_result_is_repeated(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=False):
            path = os.path.join(d, "home.json")
            result = {"down_mbps": 50.0, "up_mbps": 10.0, "latency_ms": 20.0, "tested_at": "2026-09-21T10:00:00+00:00"}
            with patch.object(r, "run_speed_test", return_value=result) as run:
                now = time.time()
                self.assertEqual(r.speed_for_report(path, now)[0], result)
                self.assertEqual(r.speed_for_report(path, now + 60)[0], result)  # a minute later: not run again
                self.assertEqual(run.call_count, 1)

    def test_a_failed_test_keeps_the_last_good_result_and_is_not_retried_at_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "home.json")
            good = {"down_mbps": 50.0, "tested_at": "2026-09-21T10:00:00+00:00"}
            now = time.time()
            with patch.object(r, "run_speed_test", return_value=good):
                r.speed_for_report(path, now - 7200)
            with patch.object(r, "run_speed_test", return_value=None) as run:
                self.assertEqual(r.speed_for_report(path, now)[0], good)  # the old result survives
                r.speed_for_report(path, now + 60)
                self.assertEqual(run.call_count, 1)  # and the broken line is not hammered

    def test_the_test_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"NIGHTJAR_SPEED": "0"}):
            with patch.object(r, "run_speed_test") as run:
                self.assertIsNone(r.speed_for_report(os.path.join(d, "home.json"), time.time())[0])
                run.assert_not_called()

    def test_a_download_that_fails_is_no_result_at_all(self):
        with patch.object(r, "measure_down", return_value=None), patch.object(r, "measure_up") as up:
            self.assertIsNone(r.run_speed_test())
            up.assert_not_called()

    def test_a_failed_upload_or_latency_is_still_a_result(self):
        with patch.object(r, "measure_down", return_value=40.0), patch.object(r, "measure_up", return_value=None), \
                patch.object(r, "measure_latency", return_value=None):
            result = r.run_speed_test()
        self.assertEqual((result["down_mbps"], result["up_mbps"], result["latency_ms"]), (40.0, None, None))


class Backup(unittest.TestCase):
    def test_not_configured_is_not_tracked(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NIGHTJAR_BACKUP_PATH", None)
            self.assertEqual(r.backup_state(), (False, None))

    def test_the_newest_file_in_a_directory_counts(self):
        with tempfile.TemporaryDirectory() as d:
            old, new = os.path.join(d, "a.sql.gz"), os.path.join(d, "b.sql.gz")
            for p, age in ((old, 5000), (new, 100)):
                open(p, "w").close()
                os.utime(p, (time.time() - age, time.time() - age))
            with patch.dict(os.environ, {"NIGHTJAR_BACKUP_PATH": d}):
                configured, at = r.backup_state()
            self.assertTrue(configured)
            self.assertIsNotNone(at)
            self.assertAlmostEqual(r.newest_mtime(d), time.time() - 100, delta=5)

    def test_configured_but_empty_or_missing_is_tracked_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            for path in (d, os.path.join(d, "nope")):
                with patch.dict(os.environ, {"NIGHTJAR_BACKUP_PATH": path}):
                    self.assertEqual(r.backup_state(), (True, None))


class Report(unittest.TestCase):
    def test_a_minimal_report(self):
        self.assertEqual(r.build_report({"cpu_count": 6}, None, False, None), {"host": {"cpu_count": 6}, "backup_configured": False})

    def test_a_full_report(self):
        net = {"down_mbps": 50.0, "tested_at": "x"}
        rep = r.build_report({"cpu_temp_c": 50.0}, net, True, "2026-09-21T02:15:00+00:00")
        self.assertEqual((rep["net"], rep["backup_configured"], rep["backup_ok_at"]), (net, True, "2026-09-21T02:15:00+00:00"))

    def test_host_stats_never_raise(self):
        self.assertIsInstance(r.host_stats(), dict)


if __name__ == "__main__":
    unittest.main()
