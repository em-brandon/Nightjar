"""Report the state of the home media server (Jellyfin) into Nightjar.

Runs on the machine that hosts Jellyfin (the ThinkCentre), because the hub lives on
a VPS and cannot reach the home network. The hub is only ever pushed to. Nightjar
watches the media server; it never plays, stores or controls anything.

Run every minute from a systemd timer (deploy/media/jellyfin-reporter.timer), or
as a long-running loop:
    python jellyfin_reporter.py --loop 60

Configuration, all from the environment:
    NIGHTJAR_URL              base URL of the hub
    NIGHTJAR_MEDIA_KEY        ingest key of the site with env "media"
    NIGHTJAR_MEDIA_SECRET     its secret, if the hub verifies signatures
    JELLYFIN_API_KEY          a Jellyfin API key (Dashboard > API Keys). It is a broad
                              admin key: it stays on this machine and is never sent
                              to the hub.
    JELLYFIN_URL              default http://127.0.0.1:8096
    NIGHTJAR_MEDIA_PATH       path whose filesystem is checked for free space (the
                              library disk). Optional: omitted, no disk is reported.

This must never break the machine it runs on, so every failure is swallowed:
short timeouts, no retries, always exit 0. If Jellyfin is down it says so (up:
false). If this reporter dies, the console shows it as silent, never as healthy.

Field names follow Jellyfin's Sessions API as documented. They have not been
checked against a live server yet, so parsing is defensive: a session that does
not look right is skipped, never allowed to fail the report.
"""

import hashlib
import hmac
import json
import os
import shutil
import sys
import time
import urllib.request
from urllib.parse import urlsplit

TIMEOUT = 10
TICKS_PER_S = 10_000_000  # Jellyfin counts time in 100-nanosecond ticks
# Only sessions active this recently. A remote-control session that is merely
# connected (a TV left on the home screen) is not watching anything.
ACTIVE_WITHIN_S = 90
MAX_SESSIONS = 40
MB = 1024**2

PLAY_METHODS = {"directplay": "direct_play", "directstream": "direct_stream", "transcode": "transcode"}


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def hub_refusal(url: str) -> str | None:
    """Why this hub address may not be used, or None when it may. https is always fine. Plain http is fine only for a
    machine talking to itself, which development needs: anywhere else it would send this host's data, readable, across
    a network. The message names the host but never the whole address, which could carry a credential. Same rule as
    client/reporter.py, copied because each of these scripts is deployed on its own."""
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    except ValueError:
        scheme, host = "", ""
    if not scheme or not host:
        return "Nightjar: the hub address is not a valid URL, so nothing will be reported. Use https://your-hub."
    if scheme == "https":
        return None
    if scheme == "http" and (host in _LOOPBACK or host.endswith(".localhost")):
        return None
    return (
        f"Nightjar: refusing to report to {scheme}://{host}: plain http would send it readable across the network. "
        "Use an https:// hub address (http:// is only accepted for localhost). Nothing will be reported."
    )


def clip(value, limit):
    if value is None:
        return None
    return str(value)[:limit]


def ticks_to_s(ticks):
    if not isinstance(ticks, int) or ticks < 0:
        return None
    return ticks // TICKS_PER_S


def parse_session(raw: dict) -> dict | None:
    """One Jellyfin session as a report entry, or None if nothing is playing."""
    item = raw.get("NowPlayingItem")
    if not isinstance(item, dict):
        return None
    play_state = raw.get("PlayState") or {}
    session_id, user, item_id, title = raw.get("Id"), raw.get("UserName"), item.get("Id"), item.get("Name")
    if not (session_id and user and item_id and title):
        return None

    season, number = item.get("ParentIndexNumber"), item.get("IndexNumber")
    episode = f"S{season:02d}E{number:02d}" if isinstance(season, int) and isinstance(number, int) else None
    method = str(play_state.get("PlayMethod") or "").replace("_", "").lower()
    return {
        "session_id": clip(session_id, 80),
        "user": clip(user, 120),
        "item_id": clip(item_id, 64),
        "title": clip(title, 200),
        "series": clip(item.get("SeriesName"), 200),
        "episode": episode,
        "kind": clip(item.get("Type"), 20),
        "client": clip(raw.get("Client"), 120),
        "device": clip(raw.get("DeviceName"), 120),
        "play_method": PLAY_METHODS.get(method),
        "paused": bool(play_state.get("IsPaused")),
        "position_s": ticks_to_s(play_state.get("PositionTicks")),
        "runtime_s": ticks_to_s(item.get("RunTimeTicks")),
    }


def build_report(sessions: list | None, version: str | None, disk: tuple[int, int] | None) -> dict:
    """`sessions` is None when Jellyfin could not be reached: that is `up: false`,
    which is different from a server that is up and has nobody watching."""
    if sessions is None:
        entries, up = [], False
    else:
        up = True
        entries = [e for e in (parse_session(s) for s in sessions if isinstance(s, dict)) if e]
    report = {"up": up, "version": clip(version, 40), "sessions": entries[:MAX_SESSIONS]}
    if disk:
        report["host_disk_total_mb"] = disk[0] // MB
        report["host_disk_free_mb"] = disk[1] // MB
    return report


def jellyfin_get(base: str, path: str, api_key: str):
    req = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": f'MediaBrowser Token="{api_key}"'}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def fetch(base: str, api_key: str) -> tuple[list | None, str | None]:
    """(sessions, version). Sessions is None if Jellyfin did not answer."""
    try:
        sessions = jellyfin_get(base, f"/Sessions?activeWithinSeconds={ACTIVE_WITHIN_S}", api_key)
    except Exception as exc:
        print(f"jellyfin_reporter: jellyfin: {type(exc).__name__}", file=sys.stderr)
        return None, None
    version = None
    try:
        version = jellyfin_get(base, "/System/Info/Public", api_key).get("Version")
    except Exception:
        pass  # the version is decoration, not worth failing the report over
    return sessions if isinstance(sessions, list) else None, version


def library_disk() -> tuple[int, int] | None:
    path = os.environ.get("NIGHTJAR_MEDIA_PATH")
    if not path:
        return None
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.total, usage.free


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A report is signed, and a redirect would carry its signature headers to wherever the answer points. The hub does not
    redirect, so any redirect is wrong: refuse it and let it surface as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_hub_opener = urllib.request.build_opener(_NoRedirect)


def post(base: str, key: str, secret: str | None, payload: dict) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        ts = str(int(time.time()))  # signed with the body, so a captured report cannot be replayed later
        headers["X-Hub-Timestamp"] = ts
        headers["X-Hub-Signature"] = (
            "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        )
    req = urllib.request.Request(f"{base}/ingest/{key}/media", data=body, headers=headers)
    _hub_opener.open(req, timeout=TIMEOUT).read()


def run_once() -> None:
    hub = os.environ["NIGHTJAR_URL"].rstrip("/")
    refusal = hub_refusal(hub)
    if refusal:
        raise ValueError(refusal)  # safe_run prints it: a refused hub must be loud, not look like a quiet host
    key = os.environ["NIGHTJAR_MEDIA_KEY"]
    secret = os.environ.get("NIGHTJAR_MEDIA_SECRET")
    base = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096").rstrip("/")
    sessions, version = fetch(base, os.environ["JELLYFIN_API_KEY"])
    post(hub, key, secret, build_report(sessions, version, library_disk()))


def safe_run() -> None:
    try:
        run_once()
    except Exception as exc:
        # Missing config, hub unreachable. Say so once and carry on: the console
        # shows a silent reporter as stale.
        print(f"jellyfin_reporter: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            safe_run()
            time.sleep(interval)
    safe_run()
    sys.exit(0)
