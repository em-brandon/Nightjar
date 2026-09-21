"""The weather feed. No database needed: the fetch is replaced, so nothing here touches the network.

    python -m unittest tests.test_weather -v

What matters is that weather can only ever be decoration: a failed fetch ends in "unavailable" or
"old", never an error, and an old reading is never presented as current.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from hub import weather  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.main import app  # noqa: E402

GOOD = {
    "elevation": 1795.0,
    "current": {"temperature_2m": 23.46, "apparent_temperature": 22.0, "weather_code": 2, "is_day": 1},
}


FORECAST = {
    "elevation": 1668.0,
    "current": {
        "time": "2026-09-21T19:00", "temperature_2m": 18.8, "weather_code": 3, "is_day": 0,
        "uv_index": 0.0, "pressure_msl": 1016.4, "surface_pressure": 839.1,
    },
    "hourly": {
        "time": [f"2026-09-21T{h:02d}:00" for h in range(16, 25 - 0)][:9],
        "precipitation_probability": [10, 20, 30, 72, 56, 45, 43, 45, 47],
        "pressure_msl": [1014.2, 1015.0, 1015.8, 1016.4, 1017.8, 1018.0, 1018.1, 1018.0, 1017.9],
    },
    "daily": {"uv_index_max": [6.4]},
}
FORECAST["hourly"]["time"] = [f"2026-09-21T{h:02d}:00" for h in range(16, 24)] + ["2026-09-22T00:00"]


class Forecast(unittest.TestCase):
    def test_rain_uv_and_pressure(self):
        p = weather.parse(FORECAST)
        self.assertEqual((p["rain_chance_pct"], p["rain_in_h"]), (72, 0))  # 72% now
        self.assertEqual((p["uv_index"], p["uv_max"]), (0.0, 6.4))
        self.assertEqual((p["pressure_hpa"], p["station_pressure_hpa"]), (1016, 839))
        self.assertEqual(p["pressure_trend"], "rising")  # 1014.2 three hours ago, 1016.4 now

    def test_rain_that_is_not_yet_likely_says_when(self):
        f = {**FORECAST, "hourly": {**FORECAST["hourly"], "precipitation_probability": [0, 0, 0, 10, 20, 55, 60, 60, 60]}}
        p = weather.parse(f)
        self.assertEqual((p["rain_chance_pct"], p["rain_in_h"]), (60, 2))

    def test_a_dry_spell_has_no_hours_to_rain(self):
        f = {**FORECAST, "hourly": {**FORECAST["hourly"], "precipitation_probability": [0] * 3 + [5, 10, 10, 5, 0, 0]}}
        self.assertEqual(weather.parse(f)["rain_in_h"], None)

    def test_falling_and_steady_pressure(self):
        def trend(now, then):
            series = list(FORECAST["hourly"]["pressure_msl"])
            series[3], series[0] = now, then
            return weather.parse({**FORECAST, "hourly": {**FORECAST["hourly"], "pressure_msl": series}})["pressure_trend"]
        self.assertEqual(trend(1010.0, 1013.0), "falling")
        self.assertEqual(trend(1013.4, 1013.0), "steady")

    def test_a_missing_forecast_never_fails_the_reading(self):
        p = weather.parse({"current": {"time": "2026-09-21T19:00", "temperature_2m": 18.8}})
        self.assertEqual((p["temperature_c"], p["rain_chance_pct"], p["pressure_trend"], p["uv_max"]), (18.8, None, None, None))


class Parse(unittest.TestCase):
    def test_a_normal_response(self):
        p = weather.parse(GOOD)
        self.assertEqual(
            (p["temperature_c"], p["feels_like_c"], p["condition"], p["icon"], p["elevation_m"]),
            (23.5, 22.0, "Partly cloudy", "partly", 1795),
        )

    def test_night_is_reported(self):
        night = {**GOOD, "current": {**GOOD["current"], "is_day": 0}}
        self.assertFalse(weather.parse(night)["is_day"])
        self.assertTrue(weather.parse(GOOD)["is_day"])

    def test_an_unknown_code_still_reports_the_temperature(self):
        p = weather.parse({"current": {"temperature_2m": 10, "weather_code": 1234}})
        self.assertEqual((p["temperature_c"], p["condition"], p["elevation_m"]), (10.0, "", None))

    def test_a_response_without_a_temperature_is_rejected(self):
        self.assertIsNone(weather.parse({"current": {}}))
        self.assertIsNone(weather.parse({}))
        self.assertIsNone(weather.parse({"current": {"temperature_2m": "warm"}}))


class Feed(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        weather._cache.update(at=0.0, value=None)
        for p in (patch.object(settings, "console_token", ""), patch.object(settings, "weather_enabled", True)):
            p.start()
            self.addCleanup(p.stop)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()

    async def get(self):
        r = await self.http.get("/api/weather")
        self.assertEqual(r.status_code, 200)  # always 200: weather must not look like a hub fault
        return r.json()

    async def test_a_reading_is_served_and_cached(self):
        with patch.object(weather, "_fetch", AsyncMock(return_value=GOOD)) as fetch:
            first = await self.get()
            await self.get()
        self.assertTrue(first["available"])
        self.assertFalse(first["stale"])
        self.assertEqual((first["temperature_c"], first["elevation_m"]), (23.5, 1795))
        # the coordinates shown are the configured ones, so the wall says where it is
        self.assertEqual((first["latitude"], first["longitude"]), (settings.weather_lat, settings.weather_lon))
        self.assertEqual(fetch.await_count, 1)  # one upstream call, however many screens

    async def test_a_failed_fetch_with_no_history_is_unavailable_not_an_error(self):
        with patch.object(weather, "_fetch", AsyncMock(side_effect=httpx.ConnectError("down"))):
            w = await self.get()
        self.assertFalse(w["available"])

    async def test_a_garbage_response_is_unavailable(self):
        with patch.object(weather, "_fetch", AsyncMock(return_value={"nope": 1})):
            self.assertFalse((await self.get())["available"])

    async def test_an_old_reading_is_marked_stale(self):
        with patch.object(weather, "_fetch", AsyncMock(return_value=GOOD)):
            await self.get()
        ttl = settings.weather_ttl_min * 60
        weather._cache["at"] -= 4 * ttl  # the upstream has been failing for a long time
        with patch.object(weather, "_fetch", AsyncMock(side_effect=httpx.ConnectError("down"))):
            w = await self.get()
        self.assertTrue(w["available"])
        self.assertTrue(w["stale"])  # last good reading, but not presented as current

    async def test_disabled_reports_unavailable_without_calling_out(self):
        with patch.object(settings, "weather_enabled", False), patch.object(weather, "_fetch", AsyncMock()) as fetch:
            self.assertFalse((await self.get())["available"])
        fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
