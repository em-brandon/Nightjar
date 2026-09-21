import asyncio
import logging
import time

import httpx
from fastapi import APIRouter

from .config import settings
from .schemas import WeatherOut

log = logging.getLogger("nightjar.weather")

# Weather is decoration on the wall display, never part of alerting. So it is fetched
# here (one call per WEATHER_TTL_MIN, however many screens are open), cached, and every
# failure ends in "unavailable" or "old", never an error that could reach the console's
# hub-health check.
router = APIRouter(prefix="/api", tags=["weather"])

URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 8

# WMO weather codes, as Open-Meteo returns them: (words, icon key for the console).
CONDITIONS = {
    0: ("Clear", "clear"),
    1: ("Mostly clear", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Fog", "fog"),
    51: ("Light drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"),
    57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"),
    81: ("Showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm with hail", "storm"),
    99: ("Thunderstorm with hail", "storm"),
}

_cache: dict = {"at": 0.0, "value": None}
_lock = asyncio.Lock()


def _number(value) -> float | None:
    try:
        return round(float(value), 1) if value is not None else None
    except (TypeError, ValueError):
        return None


def _whole(value) -> int | None:
    n = _number(value)
    return round(n) if n is not None else None


def _hour_index(data: dict) -> int | None:
    """Where the current hour sits in the hourly arrays, which start a few hours in the past."""
    try:
        now = data["current"]["time"][:13]
        return next(i for i, t in enumerate(data["hourly"]["time"]) if t[:13] == now)
    except (KeyError, TypeError, StopIteration):
        return None


def _hourly(data: dict, name: str) -> list:
    values = (data.get("hourly") or {}).get(name)
    return values if isinstance(values, list) else []


def _rain(data: dict, hour: int | None) -> tuple[int | None, int | None]:
    """(highest chance of rain in the next six hours, hours until it reaches 50%). A forecast is
    a bonus on top of the reading, so a missing or odd one gives None and never fails it."""
    if hour is None:
        return None, None
    try:
        chances = [int(p) for p in _hourly(data, "precipitation_probability")[hour : hour + 6] if p is not None]
    except (TypeError, ValueError):
        return None, None
    if not chances:
        return None, None
    return max(chances), next((i for i, p in enumerate(chances) if p >= 50), None)


def _pressure_trend(data: dict, hour: int | None) -> str | None:
    """rising, falling or steady over the last three hours. A change of 1 hPa or more counts."""
    series = _hourly(data, "pressure_msl")
    if hour is None or hour < 3 or hour >= len(series):
        return None
    now, then = _number(series[hour]), _number(series[hour - 3])
    if now is None or then is None:
        return None
    return "rising" if now - then >= 1 else "falling" if now - then <= -1 else "steady"


def _uv_max(data: dict) -> float | None:
    values = (data.get("daily") or {}).get("uv_index_max")
    return _number(values[0]) if isinstance(values, list) and values else None


def parse(data: dict) -> dict | None:
    """The fields the console shows, or None if the response is not what we expect."""
    try:
        current = data["current"]
        temperature = float(current["temperature_2m"])
        feels_like = current.get("apparent_temperature")
        code = int(current.get("weather_code", -1))
        is_day = int(current.get("is_day", 1)) == 1
        elevation = data.get("elevation")
    except (KeyError, TypeError, ValueError):
        return None
    hour = _hour_index(data)
    rain_chance, rain_in_h = _rain(data, hour)
    condition, icon = CONDITIONS.get(code, ("", "cloud"))
    return {
        "temperature_c": round(temperature, 1),
        "feels_like_c": round(float(feels_like), 1) if feels_like is not None else None,
        "condition": condition,
        "icon": icon,
        "elevation_m": round(float(elevation)) if elevation is not None else None,
        "is_day": is_day,
        "rain_chance_pct": rain_chance,
        "rain_in_h": rain_in_h,
        "uv_index": _number(current.get("uv_index")),
        "uv_max": _uv_max(data),
        "pressure_hpa": _whole(current.get("pressure_msl")),
        "pressure_trend": _pressure_trend(data, hour),
        "station_pressure_hpa": _whole(current.get("surface_pressure")),
    }


async def _fetch() -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            URL,
            params={
                "latitude": settings.weather_lat,
                "longitude": settings.weather_lon,
                "current": "temperature_2m,apparent_temperature,weather_code,is_day,uv_index,pressure_msl,surface_pressure",
                "hourly": "precipitation_probability,pressure_msl",
                "daily": "uv_index_max",
                "past_hours": 3,
                "forecast_hours": 6,
                "forecast_days": 1,
                "timezone": "auto",
            },
        )
        resp.raise_for_status()
        return resp.json()


def _out(value: dict | None, fetched_at: float, stale: bool) -> WeatherOut:
    if value is None:
        return WeatherOut(available=False, place=settings.weather_name)
    return WeatherOut(
        available=True,
        place=settings.weather_name,
        stale=stale,
        fetched_at=fetched_at,
        latitude=settings.weather_lat,
        longitude=settings.weather_lon,
        **value,
    )


@router.get("/weather", response_model=WeatherOut)
async def weather() -> WeatherOut:
    if not settings.weather_enabled:
        return WeatherOut(available=False, place=settings.weather_name)
    ttl = settings.weather_ttl_min * 60
    async with _lock:
        age = time.time() - _cache["at"]
        if _cache["value"] is None or age >= ttl:
            try:
                parsed = parse(await _fetch())
                if parsed is not None:
                    _cache.update(at=time.time(), value=parsed)
            except Exception as exc:
                # keep serving the last good reading, and say it is old
                log.warning("weather fetch failed: %s", type(exc).__name__)
        age = time.time() - _cache["at"]
        return _out(_cache["value"], _cache["at"], stale=age >= 3 * ttl)
