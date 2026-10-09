from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://hub:hub@localhost:5432/nightjar"
    verify_signatures: bool = False
    spike_threshold: int = 50
    spike_window_min: int = 10
    probe_enabled: bool = True
    probe_interval_s: int = 60
    probe_timeout_s: float = 10.0
    # Shared secret for the console and everything under /api. Empty means auth
    # is off (local development only); the hub refuses that when
    # VERIFY_SIGNATURES is on. Generate one with: openssl rand -base64 32
    console_token: str = ""
    # A stack problem resolves itself once it has been gone for this many minutes
    # of reports in a row. Long enough that a flapping stack does not open and
    # close all day, since every relapse after a resolve counts as a regression.
    stack_healthy_min: int = 10
    # How long the home media viewing history is kept. It records who watched what,
    # which is personal data, so it is bounded rather than kept forever.
    media_history_days: int = 365
    # The wall display's weather, from Open-Meteo (no key). The coordinates are where the
    # ThinkCentre and the wall are, not where the VPS is. Set them to the exact spot: the
    # elevation shown comes from the terrain model at these coordinates.
    weather_enabled: bool = True
    weather_lat: float = -1.2921
    weather_lon: float = 36.8219
    weather_name: str = "Nairobi"
    weather_ttl_min: int = 15
    # USD to KES for the wall, from open.er-api.com (no key). It updates once a day.
    rates_enabled: bool = True
    rates_ttl_min: int = 360
    # Shared secret for the presence tap (POST /presence, from an iOS Shortcuts NFC automation).
    # Empty disables the route. Generate one with: openssl rand -base64 24
    presence_token: str = ""
    # Browser reporting (POST /ingest/browser/{public_key}) has no secret to prove who is calling,
    # so it is bounded instead: per key, per visitor, and how many NEW issues one site may open
    # from browsers in an hour. A flood of varied junk is the one thing fingerprinting cannot
    # collapse, and once alerting exists every new issue is a possible notification.
    browser_rate_per_key_min: int = 120
    browser_rate_per_ip_min: int = 20
    browser_new_issues_per_hour: int = 10


settings = Settings()
