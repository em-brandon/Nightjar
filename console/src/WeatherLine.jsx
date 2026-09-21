import { Cloud, CloudFog, CloudLightning, CloudMoon, CloudRain, CloudSnow, CloudSun, Gauge, MapPin, Moon, Mountain, Sun } from "lucide-react";
import { weatherView } from "./weather.js";

// Each condition has its own colour (see .wx-icon in styles.css): a yellow sun, a pale moon, blue rain.
const ICONS = {
  sun: Sun,
  moon: Moon,
  partly: CloudSun,
  "partly-night": CloudMoon,
  cloud: Cloud,
  fog: CloudFog,
  rain: CloudRain,
  storm: CloudLightning,
  snow: CloudSnow,
};

// The wall's weather: a large coloured icon, the temperature, the condition, and beneath it the place,
// its coordinates and the altitude. `compact` is the sidebar's one-line version.
export default function WeatherLine({ weather, compact = false }) {
  const v = weatherView(weather);
  if (!v) return null;
  if (v.state === "off") {
    return <span className={`wx off ${compact ? "compact" : ""}`}>{v.text}</span>;
  }
  const Icon = ICONS[v.icon] ?? Cloud;
  const old = v.state === "old";

  if (compact) {
    return (
      <span className={`wx compact ${old ? "old" : ""}`} title={old ? "Last reading is old" : undefined}>
        <span className={`wx-icon wx-${v.icon}`}>
          <Icon size={18} strokeWidth={1.9} aria-hidden="true" />
        </span>
        <b>{v.temp}</b>
        <span className="wx-place">{v.place}</span>
        {v.elevation && <span className="wx-alt">{v.elevation}</span>}
        {old && <span className="wx-old">old</span>}
      </span>
    );
  }

  return (
    <div className={`wx ${old ? "old" : ""}`} title={old ? "Last reading is old" : v.feels ? `Feels like ${v.feels}` : undefined}>
      <span className={`wx-icon wx-${v.icon}`}>
        <Icon size={46} strokeWidth={1.6} aria-hidden="true" />
      </span>
      <span className="wx-temp">
        {v.temp}
      </span>
      <span className="wx-text">
        <span className={`wx-cond ${v.raining ? "wx-raining" : ""}`}>
          {v.condition || "—"}
          {old && <span className="wx-old">old</span>}
        </span>
        <span className="wx-meta">
          <span className="wx-place">
            <MapPin size={14} strokeWidth={1.9} aria-hidden="true" />
            {v.place}
            {v.coords && <span className="wx-coords">{v.coords}</span>}
          </span>
          {v.elevation && (
            <span className="wx-alt">
              <Mountain size={14} strokeWidth={1.9} aria-hidden="true" />
              {v.elevation}
            </span>
          )}
        </span>
      </span>
      {(v.pressure || v.uv) && (
        <span className="wx-air">
          {v.pressure && (
            <span title={v.pressure.title}>
              <Gauge size={14} strokeWidth={1.9} aria-hidden="true" />
              {v.pressure.value}
              <span className={`trend trend-${v.pressure.trend ?? "none"}`}>{v.pressure.arrow}</span>
            </span>
          )}
          {v.uv && (
            <span title={`UV index now ${v.uv.now ?? "—"}`}>
              <Sun size={14} strokeWidth={1.9} aria-hidden="true" />
              {v.uv.text}
            </span>
          )}
        </span>
      )}
    </div>
  );
}
