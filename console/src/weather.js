// Weather text helpers. Pure, so the wording can be tested without a browser.

export const fmtTemp = (c) => (c == null ? "—" : `${Math.round(c)}°C`);

export const fmtElevation = (m) => (m == null ? null : `${m.toLocaleString("en-US")} m`);

// 1.29°S 36.82°E: two decimals is about a kilometre, plenty to say where the wall is.
export function fmtCoords(lat, lon) {
  if (lat == null || lon == null) return null;
  return `${Math.abs(lat).toFixed(2)}°${lat < 0 ? "S" : "N"} ${Math.abs(lon).toFixed(2)}°${lon < 0 ? "W" : "E"}`;
}

// Which icon: a clear sky is a sun by day and a moon at night.
export function iconKey(icon, isDay) {
  if (icon === "clear") return isDay === false ? "moon" : "sun";
  if (icon === "partly") return isDay === false ? "partly-night" : "partly";
  return icon || "cloud";
}

// WHO's UV bands.
export function uvLevel(uv) {
  if (uv == null) return null;
  return uv < 3 ? "Low" : uv < 6 ? "Moderate" : uv < 8 ? "High" : uv < 11 ? "Very high" : "Extreme";
}

// Barometric pressure at sea level, with the last three hours' direction.
export function pressureView(w) {
  if (!w || !w.available || w.pressure_hpa == null) return null;
  const arrow = { rising: "↗", falling: "↘", steady: "→" }[w.pressure_trend] ?? "";
  return {
    value: `${w.pressure_hpa.toLocaleString("en-US")} hPa`,
    arrow,
    trend: w.pressure_trend ?? null,
    title: `Sea-level pressure${w.pressure_trend ? `, ${w.pressure_trend}` : ""}` +
      (w.station_pressure_hpa != null ? `. At this altitude the station itself reads ${w.station_pressure_hpa} hPa.` : ""),
  };
}

// Rain is only worth a line when it is likely right now. Otherwise the wall just states the
// conditions, so it is never dressed up as a forecast it does not have.
export function rainView(w) {
  if (!w || !w.available || w.rain_chance_pct == null || w.rain_in_h !== 0) return null;
  return { text: `Rain likely now · ${w.rain_chance_pct}%` };
}

// UV: today's peak matters more than the reading at 7 in the evening.
export function uvView(w) {
  if (!w || !w.available) return null;
  const peak = w.uv_max ?? w.uv_index;
  if (peak == null) return null;
  return { text: `UV peak ${peak.toFixed(0)} · ${uvLevel(peak)}`, level: uvLevel(peak), now: w.uv_index };
}

// What the line says, from the /api/weather feed. Weather is decoration, so a missing or old
// reading is stated plainly and never dressed up as a current one.
export function weatherView(w) {
  if (!w) return null; // the feed has not answered yet: draw nothing rather than flash a gap
  if (!w.available) return { state: "off", place: w.place, text: "weather unavailable" };
  return {
    state: w.stale ? "old" : "ok",
    place: w.place,
    temp: fmtTemp(w.temperature_c),
    feels: w.feels_like_c != null && Math.round(w.feels_like_c) !== Math.round(w.temperature_c) ? fmtTemp(w.feels_like_c) : null,
    // the sky, or "Rain likely now" when the next hour says so
    condition: rainView(w)?.text ?? (w.condition || null),
    raining: rainView(w) != null,
    pressure: pressureView(w),
    uv: uvView(w),
    icon: iconKey(w.icon, w.is_day),
    coords: fmtCoords(w.latitude, w.longitude),
    elevation: fmtElevation(w.elevation_m),
  };
}
