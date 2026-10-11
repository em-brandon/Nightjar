import os

from pydantic_settings import BaseSettings, SettingsConfigDict


def secrets_dir(path: str | None = None) -> str | None:
    """Where secrets are mounted as files, or None if there is no such directory.

    In production Docker mounts each secret as a file under /run/secrets, so no password or token sits in an
    environment variable, where `docker inspect`, process listings and crash dumps would show it. A file is named
    after the setting it fills (`console_token`, `database_url`, `pushover_token` ...) and holds only the value. An
    environment variable still wins over a file, so local development keeps using `.env`, and production simply sets
    none for the settings it mounts. A directory that is not there is ignored rather than warned about.
    """
    path = path or os.environ.get("NIGHTJAR_SECRETS_DIR", "/run/secrets")
    return path if os.path.isdir(path) else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", secrets_dir=secrets_dir())

    database_url: str = "postgresql+asyncpg://hub:hub@localhost:5432/nightjar"
    verify_signatures: bool = False
    spike_threshold: int = 50
    spike_window_min: int = 10
    probe_enabled: bool = True
    probe_interval_s: int = 60
    probe_timeout_s: float = 10.0
    # A site's outage resolves itself after this many passing checks in a row (hub/uptime.py). At the default
    # interval that is five minutes: long enough that a flapping site does not open and close an issue all day.
    uptime_recover_checks: int = 5
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
    # Quiet hours for the alert policy: only P1 makes noise inside them. The zone is named, not an offset,
    # so a move or a change of rules is one setting. Hours are local, 0 to 23, and the window may wrap midnight.
    quiet_tz: str = "Africa/Nairobi"
    quiet_start_hour: int = 22
    quiet_end_hour: int = 7
    # Pushover, the phone channel (hub/notify.py). Both are needed to send. With either empty the hub
    # still queues alerts, and each one fails visibly in the console with "not configured": an alert that
    # could not be sent must never look like an alert that was not needed.
    pushover_token: str = ""
    pushover_user: str = ""
    # The hub's public address, for the console link in a push and Pushover's acknowledgement callback.
    hub_public_url: str = ""
    # A secret in the callback path, since Pushover's callback carries no signature. Empty disables it.
    pushover_callback_token: str = ""
    # The sender loop. These are working figures to tune by breaking a staging route (decision #56), not
    # measured ones: after each failed attempt the row waits the next delay in outbox_backoff_s, and after
    # outbox_max_attempts it becomes `failed` and shows in the console.
    outbox_poll_s: float = 5.0
    outbox_max_attempts: int = 6
    outbox_backoff_s: list[int] = [30, 120, 600, 1800, 3600]
    # A row left "sending" by a crash is taken again once it has been claimed this long.
    outbox_lease_s: int = 120
    # How often the sender asks Pushover about the receipts of priority 2 pushes it has sent, to learn that
    # one was acknowledged (when the callback cannot reach the hub) or expired unanswered. Pushover asks for
    # no faster than once every 5 seconds; this is far gentler.
    receipt_poll_s: int = 30
    # Run the sender loop in this process (default true). Only the loop is switched off, never the queueing.
    outbox_enabled: bool = True
    # An external dead-man's switch (DEPLOY.md section 7, for example a healthchecks.io ping URL). The sender
    # pings it only after a pass that completed, so a sender that has died or stuck is noticed from outside
    # instead of looking like a quiet night. Empty means none. The URL is a secret: it is never logged.
    deadman_url: str = ""
    deadman_interval_s: int = 120


settings = Settings()
