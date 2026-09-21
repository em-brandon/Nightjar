"""Report the health of the home host (the ThinkCentre) into Nightjar.

Runs on the ThinkCentre itself: the hub lives on a VPS and cannot see it. The hub is only ever
pushed to. Every minute it reports the CPU temperature, load, memory, disk, uptime and the age of
the newest backup. Once an hour it also measures the internet connection, and repeats that last
result on every report until the next one.

Run every minute from a systemd timer (deploy/home/home-reporter.timer), or as a loop:
    python home_reporter.py --loop 60

Configuration, all from the environment:
    NIGHTJAR_URL               base URL of the hub
    NIGHTJAR_HOME_KEY          ingest key of the site with env "home"
    NIGHTJAR_HOME_SECRET       its secret, if the hub verifies signatures
    NIGHTJAR_DISK_PATH         filesystem to report free space for (default /)
    NIGHTJAR_INTAKE_SENSOR     a file to read the air-intake temperature from (optional)
    NIGHTJAR_EXHAUST_SENSOR    the same for the exhaust. Each is a Linux sensor file: a DS18B20's
                               /sys/bus/w1/devices/28-*/temperature, its w1_slave, or a hwmon
                               temp*_input. Omit them and the cabinet temperatures are simply not shown.
    NIGHTJAR_BACKUP_PATH       a file or directory. The newest file's modified time is "the last
                               good backup". Omit it and backups are reported as not tracked.
    NIGHTJAR_SPEED             0 turns the speed test off (default on)
    NIGHTJAR_SPEED_INTERVAL_MIN  minutes between tests (default 60)
    NIGHTJAR_SPEED_DOWN_BYTES  download size (default 5,000,000)
    NIGHTJAR_SPEED_UP_BYTES    upload size (default 1,000,000)
    NIGHTJAR_STATE             where the last test is remembered (default /var/lib/nightjar/home.json,
                               else the temp directory)

Traffic: the reporter also works out how fast data has moved over the network since its last run
(from /proc/net/dev), so the wall can show when the line is busy. It ignores loopback, Docker's
bridges and Tailscale, which would count the same bytes twice. It is a one-minute average, and the
hourly speed test shows up in it while it runs.

The speed test costs data: about 6 MB a run, 24 runs a day, roughly 5 GB a month. Small files
understate a fast line (TCP has not finished ramping up), so read the number as "is the internet
working, and roughly how well", not as a benchmark. Raise the sizes for a truer figure.

This must never break the machine it runs on, so every failure is swallowed: short timeouts, no
retries, always exit 0. If it goes quiet the console shows it as silent, never as healthy.
"""

import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 10
SPEED_TIMEOUT = 30
MB = 1024**2
# Cloudflare's speed endpoints refuse Python's default user agent (the upload answers 403).
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nightjar-home-reporter)"}
DOWN_URL = "https://speed.cloudflare.com/__down?bytes={n}"
UP_URL = "https://speed.cloudflare.com/__up"


# ---------------------------------------------------------------- host readings

def parse_meminfo(text: str) -> tuple[int, int] | None:
    """(total, available) in MiB from /proc/meminfo."""
    values = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0])  # kiB
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    return values["MemTotal"] // 1024, values["MemAvailable"] // 1024


def parse_cpu_temp(readings: list[str]) -> float | None:
    """The hottest of the given sensor files' contents, which are millidegrees C. Sensors that
    read nonsense (0, or over 150 C) are ignored rather than trusted."""
    temps = []
    for raw in readings:
        try:
            c = int(raw.strip()) / 1000
        except ValueError:
            continue
        if 1 < c < 150:
            temps.append(c)
    return round(max(temps), 1) if temps else None


def parse_sensor(text: str) -> float | None:
    """A temperature from a Linux sensor file, in degrees C. Understands a DS18B20's w1_slave (the
    reading follows `t=`, in thousandths, and the first line ends YES when its checksum passed), a
    plain integer in thousandths (hwmon, or w1's `temperature`) and a plain decimal. A reading
    outside -40..125 C, the DS18B20's own range, is None: an unplugged one reports -127."""
    text = text.strip()
    if not text:
        return None
    if "crc=" in text and not text.splitlines()[0].rstrip().endswith("YES"):
        return None  # the sensor's own checksum failed
    m = re.search(r"t=(-?\d+)", text)
    try:
        if m:
            c = int(m.group(1)) / 1000
        else:
            raw = float(text)
            c = raw / 1000 if abs(raw) > 200 else raw
    except ValueError:
        return None
    return round(c, 1) if -40 <= c <= 125 else None


def read_sensor(env_name: str) -> float | None:
    path = os.environ.get(env_name)
    text = read_text(path) if path else None
    return parse_sensor(text) if text else None


def read_text(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def read_cpu_temp() -> float | None:
    paths = glob.glob("/sys/class/thermal/thermal_zone*/temp") + glob.glob("/sys/class/hwmon/hwmon*/temp*_input")
    return parse_cpu_temp([t for t in (read_text(p) for p in paths) if t])


def host_stats() -> dict:
    stats: dict = {
        "cpu_temp_c": read_cpu_temp(),
        "intake_c": read_sensor("NIGHTJAR_INTAKE_SENSOR"),
        "exhaust_c": read_sensor("NIGHTJAR_EXHAUST_SENSOR"),
        "cpu_count": os.cpu_count(),
    }
    try:
        stats["load_1"] = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        stats["load_1"] = None  # Windows has no load average
    mem = parse_meminfo(read_text("/proc/meminfo") or "")
    if mem:
        stats["mem_total_mb"], stats["mem_available_mb"] = mem
    uptime = read_text("/proc/uptime")
    if uptime:
        try:
            stats["uptime_s"] = int(float(uptime.split()[0]))
        except (ValueError, IndexError):
            pass
    try:
        usage = shutil.disk_usage(os.environ.get("NIGHTJAR_DISK_PATH", "/"))
        stats["disk_total_mb"], stats["disk_free_mb"] = usage.total // MB, usage.free // MB
    except OSError:
        pass
    return {k: v for k, v in stats.items() if v is not None}


def newest_mtime(path: str) -> float | None:
    """Modified time of the file, or of the newest file inside the directory."""
    try:
        if os.path.isdir(path):
            times = [os.path.getmtime(os.path.join(path, n)) for n in os.listdir(path)
                     if os.path.isfile(os.path.join(path, n))]
            return max(times) if times else None
        return os.path.getmtime(path)
    except OSError:
        return None


def backup_state() -> tuple[bool, str | None]:
    """(is a backup path configured, when the newest backup was written)."""
    path = os.environ.get("NIGHTJAR_BACKUP_PATH")
    if not path:
        return False, None
    mtime = newest_mtime(path)
    return True, (datetime.fromtimestamp(mtime, timezone.utc).isoformat() if mtime else None)


# ---------------------------------------------------------------- network traffic

# Interfaces whose bytes are either not the home connection or would be counted twice: loopback,
# Docker's and libvirt's bridges and virtual pairs, and Tailscale (its packets also cross the real
# interface).
IGNORED_IFACES = ("lo", "docker", "veth", "br-", "virbr", "tailscale")


def parse_net_dev(text: str) -> tuple[int, int] | None:
    """(bytes received, bytes sent) added up over the real interfaces, from /proc/net/dev."""
    rx = tx = 0
    found = False
    for line in text.splitlines()[2:]:  # two header lines
        name, _, rest = line.partition(":")
        name, fields = name.strip(), rest.split()
        if not name or len(fields) < 9 or name.startswith(IGNORED_IFACES):
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except ValueError:
            continue
        found = True
    return (rx, tx) if found else None


# A run is normally a minute after the last. After a long gap (a reboot, a stopped timer) the
# average would be over hours and say nothing about now.
MAX_TRAFFIC_GAP_S = 300


def traffic_rates(prev: dict | None, rx: int, tx: int, now: float) -> tuple[float, float] | None:
    """Bits per second received and sent since the last run, or None when there is no fair answer:
    the first run, a long gap, a clock that went backwards, or counters that reset (a reboot)."""
    if not prev:
        return None
    try:
        elapsed = now - float(prev["at"])
        d_rx, d_tx = rx - int(prev["rx"]), tx - int(prev["tx"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < elapsed <= MAX_TRAFFIC_GAP_S or d_rx < 0 or d_tx < 0:
        return None
    return round(d_rx * 8 / elapsed, 1), round(d_tx * 8 / elapsed, 1)


def network_activity(state: dict, now: float) -> dict:
    """{rx_bps, tx_bps} for the report, updating the remembered counters in `state`."""
    current = parse_net_dev(read_text("/proc/net/dev") or "")
    if current is None:
        return {}
    rates = traffic_rates(state.get("counters"), current[0], current[1], now)
    state["counters"] = {"at": now, "rx": current[0], "tx": current[1]}
    return {"rx_bps": rates[0], "tx_bps": rates[1]} if rates else {}


# ---------------------------------------------------------------- speed test

def mbps(nbytes: int, seconds: float) -> float | None:
    if nbytes <= 0 or seconds <= 0:
        return None
    return round(nbytes * 8 / seconds / 1e6, 2)


def is_due(state: dict, now: float, interval_min: int) -> bool:
    """A test is due when the last ATTEMPT was long enough ago. Attempts count, not just successes:
    a broken connection must not be hammered every minute."""
    last = state.get("attempted_at")
    return not isinstance(last, (int, float)) or now - last >= interval_min * 60 or now < last


def _open(req: urllib.request.Request):
    return urllib.request.urlopen(req, timeout=SPEED_TIMEOUT)


def measure_latency(samples: int = 3) -> float | None:
    times = []
    for _ in range(samples):
        start = time.monotonic()
        try:
            _open(urllib.request.Request(DOWN_URL.format(n=0), headers=HEADERS)).read()
        except Exception:
            continue
        times.append((time.monotonic() - start) * 1000)
    return round(statistics.median(times), 1) if times else None


def measure_down(nbytes: int) -> float | None:
    start = time.monotonic()
    try:
        got = len(_open(urllib.request.Request(DOWN_URL.format(n=nbytes), headers=HEADERS)).read())
    except Exception:
        return None
    return mbps(got, time.monotonic() - start)


def measure_up(nbytes: int) -> float | None:
    req = urllib.request.Request(UP_URL, data=b"\0" * nbytes, method="POST", headers=HEADERS)
    start = time.monotonic()
    try:
        _open(req).read()
    except Exception:
        return None
    return mbps(nbytes, time.monotonic() - start)


def run_speed_test() -> dict | None:
    """One test, or None if the connection did not answer at all. Upload and latency are optional:
    a working download with a failed upload is still a result."""
    down = measure_down(int(os.environ.get("NIGHTJAR_SPEED_DOWN_BYTES", "5000000")))
    if down is None:
        return None
    return {
        "down_mbps": down,
        "up_mbps": measure_up(int(os.environ.get("NIGHTJAR_SPEED_UP_BYTES", "1000000"))),
        "latency_ms": measure_latency(),
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }


def state_path() -> str:
    preferred = os.environ.get("NIGHTJAR_STATE", "/var/lib/nightjar/home.json")
    directory = os.path.dirname(preferred) or "."
    if os.path.isdir(directory) and os.access(directory, os.W_OK):
        return preferred
    return os.path.join(tempfile.gettempdir(), "nightjar-home.json")


def load_state(path: str) -> dict:
    try:
        with open(path) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w") as fh:
            json.dump(state, fh)
    except OSError as exc:
        print(f"home_reporter: could not save state: {exc}", file=sys.stderr)


def speed_for_report(path: str, now: float) -> tuple[dict | None, dict]:
    """(the last test result, this run's traffic), running a new test first if one is due."""
    state = load_state(path)
    traffic = network_activity(state, now)
    if os.environ.get("NIGHTJAR_SPEED", "1") != "0" and is_due(
        state, now, int(os.environ.get("NIGHTJAR_SPEED_INTERVAL_MIN", "60"))
    ):
        state["attempted_at"] = now
        result = run_speed_test()
        if result is not None:
            state["last"] = result
    save_state(path, state)
    return state.get("last"), traffic


# ---------------------------------------------------------------- reporting

def build_report(host: dict, net: dict | None, backup_configured: bool, backup_ok_at: str | None) -> dict:
    report: dict = {"host": host, "backup_configured": backup_configured}
    if net:
        report["net"] = net
    if backup_ok_at:
        report["backup_ok_at"] = backup_ok_at
    return report


def post(base: str, key: str, secret: str | None, payload: dict) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{base}/ingest/{key}/home", data=body, headers=headers)
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def run_once() -> None:
    hub = os.environ["NIGHTJAR_URL"].rstrip("/")
    key = os.environ["NIGHTJAR_HOME_KEY"]
    secret = os.environ.get("NIGHTJAR_HOME_SECRET")
    configured, backup_at = backup_state()
    net, traffic = speed_for_report(state_path(), time.time())
    post(hub, key, secret, build_report({**host_stats(), **traffic}, net, configured, backup_at))


def safe_run() -> None:
    try:
        run_once()
    except Exception as exc:
        # Missing config, hub unreachable. Say so once and carry on: the console shows a silent
        # reporter as stale.
        print(f"home_reporter: {type(exc).__name__}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--loop") + 1])
        while True:
            safe_run()
            time.sleep(interval)
    safe_run()
    sys.exit(0)
