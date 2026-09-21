// Weather wording. Run with: npm test

import assert from "node:assert/strict";
import { fmtCoords, fmtElevation, fmtTemp, iconKey, pressureView, rainView, uvLevel, uvView, weatherView } from "./weather.js";

const ok = (name) => console.log(`  ok   ${name}`);
const reading = (over = {}) => ({
  available: true, stale: false, place: "Nairobi", temperature_c: 23.6, feels_like_c: 22.1,
  condition: "Partly cloudy", icon: "partly", elevation_m: 1668, ...over,
});

assert.equal(fmtTemp(23.6), "24°C");
assert.equal(fmtTemp(null), "—");
assert.equal(fmtElevation(1668), "1,668 m");
assert.equal(fmtElevation(null), null);
ok("temperature and elevation formatting");

assert.equal(weatherView(null), null, "before the feed answers, draw nothing");
assert.equal(weatherView({ available: false, place: "Nairobi" }).state, "off");
ok("no feed and an unavailable feed");

const v = weatherView(reading());
assert.deepEqual([v.state, v.temp, v.feels, v.elevation, v.condition], ["ok", "24°C", "22°C", "1,668 m", "Partly cloudy"]);
assert.equal(weatherView(reading({ feels_like_c: 23.8 })).feels, null, "feels-like is hidden when it is the same number");
ok("a normal reading");

assert.equal(weatherView(reading({ stale: true })).state, "old", "an old reading is never shown as current");
ok("a stale reading is marked old");

assert.equal(fmtCoords(-1.2921, 36.8219), "1.29°S 36.82°E");
assert.equal(fmtCoords(51.5, -0.12), "51.50°N 0.12°W");
assert.equal(fmtCoords(null, 3), null);
ok("coordinates read as a map reference");

assert.equal(iconKey("clear", true), "sun");
assert.equal(iconKey("clear", false), "moon", "a clear night is a moon, not a sun");
assert.equal(iconKey("partly", false), "partly-night");
assert.equal(iconKey("rain", false), "rain");
assert.equal(iconKey(undefined, true), "cloud");
ok("sun by day, moon by night");

assert.equal(weatherView(reading({ latitude: -1.29, longitude: 36.82, is_day: false, icon: "clear" })).icon, "moon");
assert.equal(weatherView(reading({ latitude: -1.29, longitude: 36.82 })).coords, "1.29°S 36.82°E");
ok("the view carries the coordinates and the night icon");

assert.deepEqual([uvLevel(2.9), uvLevel(3), uvLevel(6), uvLevel(8), uvLevel(11), uvLevel(null)], ["Low", "Moderate", "High", "Very high", "Extreme", null]);
assert.equal(uvView(reading({ uv_max: 6.4, uv_index: 0 })).text, "UV peak 6 · High");
assert.equal(uvView(reading({ uv_max: null, uv_index: 2 })).text, "UV peak 2 · Low", "falls back to the current reading");
assert.equal(uvView(reading({ uv_max: null, uv_index: null })), null);
ok("UV bands");

assert.equal(rainView(reading({ rain_chance_pct: 72, rain_in_h: 0 })).text, "Rain likely now · 72%");
assert.equal(rainView(reading({ rain_chance_pct: 60, rain_in_h: 2 })), null, "rain later is not rain now");
assert.equal(rainView(reading({ rain_chance_pct: 10, rain_in_h: null })), null);
assert.equal(rainView(reading({ rain_chance_pct: null })), null, "no forecast is not 'dry'");
assert.equal(rainView({ available: false }), null);
ok("rain only when it is likely now");

// the condition line: rain likely now, otherwise the sky
assert.equal(weatherView(reading({ rain_chance_pct: 72, rain_in_h: 0 })).condition, "Rain likely now · 72%");
assert.equal(weatherView(reading({ rain_chance_pct: 72, rain_in_h: 0 })).raining, true);
assert.equal(weatherView(reading({ rain_chance_pct: 60, rain_in_h: 2 })).condition, "Partly cloudy");
assert.equal(weatherView(reading({ rain_chance_pct: 60, rain_in_h: 2 })).raining, false);
assert.equal(weatherView(reading()).condition, "Partly cloudy", "no forecast at all: just the conditions");
ok("the condition line says rain when it is likely now, else the conditions");

const pv = pressureView(reading({ pressure_hpa: 1016, pressure_trend: "rising", station_pressure_hpa: 839 }));
assert.deepEqual([pv.value, pv.arrow, pv.trend], ["1,016 hPa", "↗", "rising"]);
assert.match(pv.title, /839 hPa/);
assert.equal(pressureView(reading({ pressure_hpa: 1010, pressure_trend: "falling" })).arrow, "↘");
assert.equal(pressureView(reading({ pressure_hpa: 1010, pressure_trend: null })).arrow, "");
assert.equal(pressureView(reading({ pressure_hpa: null })), null);
ok("pressure and its trend");

console.log("\nweather rules passed");
