"""The alert policy: given a state change, which channels fire and how loudly.

Pure functions with no database and no network, so every cell of the routing matrix in CLAUDE.md can
be tested exactly. The outbox (hub/outbox.py) calls this when it writes a notification.

The one rule that governs it (CLAUDE.md, decision #8): **nothing here may suppress an alert.** Presence
and quiet hours choose the channels and the loudness. They never choose whether a P1 or P2 is sent, and
if the policy itself fails, `decide_safely` falls back to a normal push.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

log = logging.getLogger("nightjar.policy")

# The changes that can alert at all. `None` (a known issue, already open) never does.
ALERTING_CHANGES = ("new", "regression", "spike")

# Pushover priorities: 2 repeats until acknowledged, 0 is a normal push, -1 is delivered without sound or
# vibration, so a P2 at night waits in the notification list instead of waking anyone.
EMERGENCY = 2
NORMAL = 0
QUIET = -1


@dataclass(frozen=True)
class Decision:
    push: bool
    priority: int
    # The cabinet speaker adds to the push and never replaces it. "alarm" for a P1, "chime" for a P2.
    speaker: str | None
    # P3 goes to the morning digest instead of the phone.
    digest_only: bool
    reason: str


def is_quiet(now: datetime, tz: str | None = None, start: int | None = None, end: int | None = None) -> bool:
    """Whether `now` falls inside quiet hours, in the configured zone. The window may wrap midnight
    (22 to 7), and `start` is inside it while `end` is not: 22:00 is quiet, 07:00 is not."""
    tz = tz or settings.quiet_tz
    start = settings.quiet_start_hour if start is None else start
    end = settings.quiet_end_hour if end is None else end
    hour = now.astimezone(ZoneInfo(tz)).hour
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def decide(severity: str, change: str | None, presence: str | None, now: datetime) -> Decision:
    """The routing matrix. `presence` is the effective state ("home" or "away"), where anything
    unrecognised counts as away: the state that uses only the reliable channel."""
    if change not in ALERTING_CHANGES:
        return Decision(False, NORMAL, None, False, "no state change, nothing to send")

    home = presence == "home"
    quiet = is_quiet(now)

    if severity == "P3":
        return Decision(False, NORMAL, None, True, "P3 goes to the digest")

    if severity == "P1":
        # Wakes the phone at any hour, whoever is home, and repeats until acknowledged.
        reason = "P1: priority 2 push" + (" and speaker" if home else "")
        return Decision(True, EMERGENCY, "alarm" if home else None, False, reason)

    if severity == "P2":
        if quiet:
            # Silent at night regardless of presence: still delivered, so nothing is lost, just not loud.
            return Decision(True, QUIET, None, False, "P2 inside quiet hours: push without sound")
        reason = "P2: push" + (" and soft chime" if home else "")
        return Decision(True, NORMAL, "chime" if home else None, False, reason)

    # An unknown severity is not a reason to say nothing.
    return Decision(True, NORMAL, None, False, f"unrecognised severity {severity!r}: push at normal priority")


def decide_safely(severity: str, change: str | None, presence: str | None, now: datetime) -> Decision:
    """`decide`, but if it raises for any reason the answer is a normal push. A bug in the policy must
    never turn into silence."""
    try:
        return decide(severity, change, presence, now)
    except Exception:
        log.exception("alert policy failed; sending a normal push")
        return Decision(True, NORMAL, None, False, "policy error: push at normal priority")
