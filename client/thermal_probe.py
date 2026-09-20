"""Post host thermals into Nightjar so the cabinet cannot cook the machine quietly.

Run from cron or a systemd timer every 5 minutes:
    */5 * * * * /opt/nightjar/.venv/bin/python /opt/nightjar/thermal_probe.py

Reports nothing while temperatures are healthy. That is deliberate — this is a
state-change reporter, not a metrics exporter. If you want graphs, point
Grafana at Postgres.
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import urllib.request

INGEST_URL = os.environ["NIGHTJAR_INGEST_URL"]      # .../ingest/<key>/event
INGEST_SECRET = os.environ["NIGHTJAR_INGEST_SECRET"]
NVME_DEVICE = os.environ.get("NVME_DEVICE", "/dev/nvme0n1")

# Sustained thresholds. Deliberately below the hardware's own limits so you
# hear about it while it is still a ticket and not yet a shutdown.
CPU_WARN, CPU_CRIT = 80, 95      # Tjmax on the i5-8500T is 100
NVME_WARN, NVME_CRIT = 65, 75    # M.2 drives self-throttle around 70


def cpu_temp() -> float | None:
    try:
        out = subprocess.run(
            ["sensors", "-u"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hits = [float(m) for m in re.findall(r"temp\d+_input:\s*([\d.]+)", out)]
    return max(hits) if hits else None


def nvme_temp() -> float | None:
    try:
        out = subprocess.run(
            ["nvme", "smart-log", NVME_DEVICE],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^temperature\s*:\s*(\d+)", out, re.MULTILINE)
    return float(m.group(1)) if m else None


def throttle_count() -> int:
    total = 0
    base = "/sys/devices/system/cpu"
    for entry in os.listdir(base):
        path = f"{base}/{entry}/thermal_throttle/core_throttle_count"
        try:
            with open(path) as fh:
                total += int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return total


def report(exception_type: str, message: str, level: str, extra: dict) -> None:
    body = json.dumps(
        {
            "exception_type": exception_type,
            "message": message,
            "frames": [
                {"module": "thermal_probe", "function": "check", "lineno": 1}
            ],
            "level": level,
            "environment": "infrastructure",
            "extra": extra,
        }
    ).encode()

    signature = "sha256=" + hmac.new(
        INGEST_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()

    request = urllib.request.Request(
        INGEST_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": signature,
        },
    )
    urllib.request.urlopen(request, timeout=5).read()


def main() -> None:
    cpu, nvme, throttles = cpu_temp(), nvme_temp(), throttle_count()
    extra = {"cpu_c": cpu, "nvme_c": nvme, "throttle_count": throttles}

    # Note the fixed message strings: the fingerprint groups all occurrences of
    # "cabinet running hot" into one issue rather than one per reading. The
    # actual numbers live in `extra`, which does not affect the hash.
    if cpu is not None and cpu >= CPU_CRIT:
        report("ThermalCritical", "CPU at critical temperature", "fatal", extra)
    elif nvme is not None and nvme >= NVME_CRIT:
        report("ThermalCritical", "NVMe at critical temperature", "fatal", extra)
    elif cpu is not None and cpu >= CPU_WARN:
        report("ThermalWarning", "CPU running hot in enclosure", "error", extra)
    elif nvme is not None and nvme >= NVME_WARN:
        report("ThermalWarning", "NVMe running hot in enclosure", "error", extra)

    if throttles > 0:
        report(
            "ThermalThrottle",
            "CPU has throttled since boot",
            "error",
            extra,
        )


if __name__ == "__main__":
    main()
