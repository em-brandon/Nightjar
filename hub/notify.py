"""The push adapter. The only file that knows which provider sends the phone's notifications.

Everything else calls `send(notification)`. If Pushover is ever replaced, this is the one file that
changes (CLAUDE.md). It raises, and does not swallow: the outbox (hub/outbox.py) decides what a failure
means, so a push that could not be sent is recorded and retried and shown, never quietly lost.
"""

from dataclasses import dataclass

import httpx

from .config import settings
from .models import Notification

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TIMEOUT_S = 10.0

# Priority 2 repeats every `retry` seconds until acknowledged or `expire` seconds pass (Pushover's
# minimum retry is 30 and the maximum expire is 10800). The hub runs no retry timer of its own for P1.
EMERGENCY_RETRY_S = 60
EMERGENCY_EXPIRE_S = 1800

CHANGE_WORDS = {"new": "new", "regression": "is back", "spike": "is spiking"}


class NotConfigured(Exception):
    """Pushover has no token or user. Retrying cannot help, so the outbox fails the row at once."""


class SendError(Exception):
    """The push did not go. `retryable` says whether trying again could help: a timeout, a rate limit
    or a 5xx might clear; a rejected token or a bad request will not."""

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Sent:
    # Pushover's receipt for a priority 2 push, which it calls back about on acknowledgement
    receipt: str | None


def configured() -> bool:
    return bool(settings.pushover_token and settings.pushover_user)


def callback_url() -> str | None:
    if not (settings.hub_public_url and settings.pushover_callback_token):
        return None
    return f"{settings.hub_public_url.rstrip('/')}/pushover/ack/{settings.pushover_callback_token}"


def build(n: Notification) -> dict[str, str | int]:
    """The form fields for one push. Never includes anything that is not about the incident."""
    fields: dict[str, str | int] = {
        "token": settings.pushover_token,
        "user": settings.pushover_user,
        # a test push (the console's Send a test push button) is not about an incident, so it has no severity or change
        "title": (n.site if n.change == "test" else f"{n.severity} {n.site}")[:250],
        "message": (n.title if n.change == "test" else f"{n.title} ({CHANGE_WORDS.get(n.change, n.change)})")[:1024],
        "priority": n.priority,
    }
    if settings.hub_public_url:
        fields["url"] = f"{settings.hub_public_url.rstrip('/')}/#/incidents"
        fields["url_title"] = "Open the console"
    if n.priority == 2:
        fields["retry"] = EMERGENCY_RETRY_S
        fields["expire"] = EMERGENCY_EXPIRE_S
        fields["sound"] = "siren"
        cb = callback_url()
        if cb:
            fields["callback"] = cb
    return fields


async def send(n: Notification, client: httpx.AsyncClient | None = None) -> Sent:
    """Send one notification. Raises NotConfigured or SendError, never returns on failure."""
    if not configured():
        raise NotConfigured("Pushover is not configured (set PUSHOVER_TOKEN and PUSHOVER_USER)")

    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        try:
            res = await client.post(PUSHOVER_URL, data=build(n))
        except httpx.TimeoutException as err:
            raise SendError("Pushover timed out", retryable=True) from err
        except httpx.HTTPError as err:
            # the class name only: the message can carry the request URL
            raise SendError(f"Pushover unreachable ({type(err).__name__})", retryable=True) from err
    finally:
        if own:
            await client.aclose()

    # 5xx and 429 may clear on their own. Any other 4xx means the request itself was refused (a bad
    # token, an invalid user), which resending identically cannot fix.
    if res.status_code >= 500 or res.status_code == 429:
        raise SendError(f"Pushover answered {res.status_code}", retryable=True)

    try:
        body = res.json()
    except ValueError:
        raise SendError(f"Pushover answered {res.status_code} with something that was not JSON", retryable=res.status_code >= 500) from None

    if res.status_code != 200 or body.get("status") != 1:
        errors = body.get("errors") if isinstance(body.get("errors"), list) else []
        reason = "; ".join(str(e) for e in errors)[:200] or f"status {res.status_code}"
        raise SendError(f"Pushover refused it: {reason}", retryable=False)

    receipt = body.get("receipt")
    return Sent(receipt=str(receipt) if receipt else None)
