"""Report the state of each staging stack into Nightjar.

Runs on the machine that hosts staging (the ThinkCentre), because the hub lives
on a VPS and can neither see this machine's Docker nor be trusted with it. The
hub is only ever pushed to. Nothing here gives the internet a way to touch a
container.

Run every minute from cron or a systemd timer:
    * * * * * /opt/nightjar/.venv/bin/python /opt/nightjar/stack_reporter.py

or as a long-running loop:
    python stack_reporter.py --loop 60

Configuration:
    NIGHTJAR_URL      base URL of the hub, e.g. https://nightjar.example.co.ke
    NIGHTJAR_STACKS   path to a JSON file (default ./stacks.json) mapping each
                      Docker Compose project name to its staging site's ingest
                      key, and its secret if the hub verifies signatures:

        {"agrovet": {"key": "abc...", "secret": "def..."}}

    NIGHTJAR_DISK_PATH  (optional) path whose filesystem is checked for free space.
                      Defaults to /var/lib/docker if it exists, else the root.

Only the projects listed there are reported, so unrelated containers on the same
machine never leak into the console.

This must never break the machine it runs on, so every failure is swallowed:
short timeouts, no retries, always exit 0. If it goes quiet the console says so
(a stale report is shown as stale, never as healthy).
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone

TIMEOUT = 10
# A container with no limit reports the host's whole memory as its limit.
UNLIMITED_FRACTION = 0.95
MIB = 1024**2  # docker prints MiB, so report MiB, not decimal megabytes

# Exit codes that mean "something stopped it", not "it crashed": a clean exit,
# SIGKILL, SIGTERM, and 255. Docker Desktop stopping its VM, a host reboot and
# `docker compose stop` all produce these, and none is a failure. A real crash
# is 1, 2, 139 (segfault), 134 (abort) and so on.
STOP_CODES = {0, 137, 143, 255}
# A crash older than this is a container that is simply down, not a fresh
# failure. The crash was reported when it happened (as an issue) and is not
# re-announced for ever. The note on the service still says what happened.
CRASH_WINDOW = int(os.environ.get("NIGHTJAR_CRASH_WINDOW_MIN", "15")) * 60

_UNITS = {
    "b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}


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


def parse_size(text: str) -> float:
    """'27.12MiB' -> bytes. Anything unparseable is 0 rather than an error."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]*)", text or "")
    if not m:
        return 0.0
    return float(m.group(1)) * _UNITS.get(m.group(2).lower() or "b", 1)


def parse_labels(text: str) -> dict:
    """docker prints labels as 'a=1,b=2'."""
    out = {}
    for part in (text or "").split(","):
        key, _, value = part.partition("=")
        if key:
            out[key] = value
    return out


def docker_lines(*args: str) -> list[dict]:
    result = subprocess.run(
        ["docker", *args, "--format", "{{json .}}"],
        capture_output=True, text=True, timeout=TIMEOUT, check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def host_memory_bytes() -> int:
    result = subprocess.run(
        ["docker", "info", "--format", "{{.MemTotal}}"],
        capture_output=True, text=True, timeout=TIMEOUT, check=True,
    )
    return int(result.stdout.strip() or 0)


def volume_sizes() -> dict[str, float] | None:
    """Bytes of volumes per Compose project, from one `docker system df -v`.
    None if it cannot be read: a failed lookup must not become a reported 0."""
    try:
        result = subprocess.run(
            ["docker", "system", "df", "-v", "--format", "json"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        volumes = json.loads(result.stdout).get("Volumes") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    totals: dict[str, float] = {}
    for v in volumes:
        project = parse_labels(v.get("Labels", "")).get("com.docker.compose.project")
        if project:
            totals[project] = totals.get(project, 0.0) + parse_size(v.get("Size", ""))
    return totals


def parse_time(text: str) -> float | None:
    """Docker's RFC 3339 timestamp (nanoseconds, trailing Z) -> epoch seconds.
    Python before 3.11 cannot read a bare Z, and none can read 9 fractional digits."""
    text = (text or "").strip()
    if not text or text.startswith("0001-01-01"):
        return None  # "never finished"
    text = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def inspect_containers(container_ids: list[str]) -> dict[str, dict]:
    """Restart count, exit code, out-of-memory flag and finish time, by name."""
    if not container_ids:
        return {}
    fmt = "{{.Name}}|{{.RestartCount}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.FinishedAt}}"
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", fmt, *container_ids],
            capture_output=True, text=True, timeout=TIMEOUT, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    info = {}
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        name, restarts, code, oom, finished = parts
        info[name.lstrip("/")] = {
            "restarts": int(restarts) if restarts.isdigit() else None,
            "exit_code": int(code) if code.lstrip("-").isdigit() else None,
            "oom": oom.strip().lower() == "true",
            "finished_at": parse_time(finished),
        }
    return info


def host_disk() -> tuple[int, int] | None:
    """(total, free) bytes of the filesystem Docker writes to."""
    path = os.environ.get("NIGHTJAR_DISK_PATH") or (
        "/var/lib/docker" if os.path.isdir("/var/lib/docker") else os.path.abspath(os.sep)
    )
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.total, usage.free


def humanize(seconds: float) -> str:
    seconds = int(max(0, seconds))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def service_state(container: dict, info: dict | None = None, now: float | None = None) -> tuple[str, str | None]:
    """(state, note). state is running | stopped | unhealthy.

    Telling a stop from a crash is the whole point. A container that exited with
    137, 143 or 255 was stopped (Docker Desktop's VM, a reboot, `compose stop`),
    which is normal and must not raise an alarm. A real crash code is unhealthy
    while it is fresh; once it is old the container is simply down, and the note
    keeps the facts visible either way."""
    now = time.time() if now is None else now
    state, status = container.get("State", ""), container.get("Status", "")
    if state == "running":
        return ("unhealthy", "failing its health check") if "(unhealthy)" in status else ("running", None)
    if state == "restarting":
        return "unhealthy", "restarting"
    if state == "dead":
        return "unhealthy", "dead"
    if state != "exited":
        return "stopped", None  # created, paused, removing

    info = info or {}
    code, oom, finished = info.get("exit_code"), info.get("oom"), info.get("finished_at")
    if code is None:  # no inspect data: fall back to the status text
        m = re.search(r"Exited \((\d+)\)", status)
        code = int(m.group(1)) if m else None
    when = f", {humanize(now - finished)}" if finished else ""
    fresh = finished is not None and now - finished <= CRASH_WINDOW

    if oom:
        note = f"killed for running out of memory{when}"
        return ("unhealthy" if fresh else "stopped"), note
    if code in STOP_CODES:
        return "stopped", (None if code == 0 else f"exited {code} (stopped or killed){when}")
    return ("unhealthy" if fresh else "stopped"), f"crashed with exit code {code}{when}"


def stack_state(states: list[str]) -> str:
    if not states:
        return "stopped"
    if "unhealthy" in states:
        return "degraded"
    if all(s == "running" for s in states):
        return "running"
    if all(s == "stopped" for s in states):
        return "stopped"
    return "degraded"  # some up, some down


def collect(projects: list[str]) -> dict[str, dict]:
    host = host_memory_bytes()
    stats = {row["Name"]: row for row in docker_lines("stats", "--no-stream")}

    by_project: dict[str, list[dict]] = {p: [] for p in projects}
    for c in docker_lines("ps", "-a", "--size"):
        labels = parse_labels(c.get("Labels", ""))
        project = labels.get("com.docker.compose.project")
        if project in by_project:
            c["_service"] = labels.get("com.docker.compose.service", c.get("Names", "?"))
            by_project[project].append(c)

    vols = volume_sizes()
    info = inspect_containers([c["ID"] for cs in by_project.values() for c in cs])
    now = time.time()
    disk = host_disk()

    reports = {}
    for project, containers in by_project.items():
        services, mem, cpu, limits, layers = [], 0.0, 0.0, [], 0.0
        for c in containers:
            st = stats.get(c.get("Names", ""))
            used = None
            # "20.5kB (virtual 285MB)": the first figure is this container's own
            # writable layer; the virtual part is the shared image, not counted.
            layers += parse_size((c.get("Size") or "").split(" (")[0])
            if st:
                used_raw, _, limit_raw = st.get("MemUsage", "").partition("/")
                used = parse_size(used_raw)
                mem += used
                limit = parse_size(limit_raw)
                limits.append(limit if limit < host * UNLIMITED_FRACTION else None)
                cpu += float((st.get("CPUPerc") or "0").strip("%") or 0)
            meta = info.get(c.get("Names", ""), {})
            state, note = service_state(c, meta, now)
            services.append({
                "name": c["_service"],
                "state": state,
                "mem_mb": round(used / MIB) if used is not None else None,
                "restarts": meta.get("restarts"),
                "note": note,
            })
        # A stack only has a memory limit if every running container has one.
        # One unlimited service means one runaway can still take the host.
        limited = limits and all(x is not None for x in limits)
        reports[project] = {
            "stack": project,
            "state": stack_state([s["state"] for s in services]),
            "mem_mb": round(mem / MIB),
            "mem_limit_mb": round(sum(limits) / MIB) if limited else None,
            "cpu_pct": round(cpu, 1),
            "host_mem_mb": round(host / MIB),
            # volumes plus writable layers; None if volumes could not be read
            "disk_mb": round((vols.get(project, 0.0) + layers) / MIB) if vols is not None else None,
            "restarts": sum(s["restarts"] or 0 for s in services) if info else None,
            "host_disk_total_mb": round(disk[0] / MIB) if disk else None,
            "host_disk_free_mb": round(disk[1] / MIB) if disk else None,
            "services": services,
        }
    return reports


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
    req = urllib.request.Request(f"{base}/ingest/{key}/stack", data=body, headers=headers)
    _hub_opener.open(req, timeout=TIMEOUT).read()


def run_once() -> None:
    base = os.environ["NIGHTJAR_URL"].rstrip("/")
    refusal = hub_refusal(base)
    if refusal:
        raise ValueError(refusal)  # safe_run prints it: a refused hub must be loud, not look like a quiet host
    with open(os.environ.get("NIGHTJAR_STACKS", "stacks.json")) as fh:
        config = json.load(fh)
    for project, report in collect(list(config)).items():
        try:
            post(base, config[project]["key"], config[project].get("secret"), report)
        except Exception as exc:  # one bad stack must not stop the others
            print(f"stack_reporter: {project}: {type(exc).__name__}", file=sys.stderr)


def safe_run() -> None:
    try:
        run_once()
    except Exception as exc:
        # Docker missing, config unreadable, hub unreachable. Say so once and
        # carry on: the console shows a silent reporter as stale.
        print(f"stack_reporter: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            safe_run()
            time.sleep(interval)
    safe_run()
    sys.exit(0)
