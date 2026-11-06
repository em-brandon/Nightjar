"""Keeps secrets that live in a URL path out of the hub's access log (security audit, low-severity item).

Pushover's acknowledgement callback carries no signature, so its secret is part of the path (`/pushover/ack/<token>`, see
hub/pushover.py). uvicorn's access log writes the path of every request, which would put that secret into `docker logs` and
whatever collects them. Caddy's own log is handled in deploy/Caddyfile.
"""

import logging
import re

SECRET_PATHS = re.compile(r"(/pushover/ack/)[^\s?\"]+")
REDACTED = "REDACTED"


def redact(text: str) -> str:
    return SECRET_PATHS.sub(rf"\1{REDACTED}", text)


class RedactSecretPaths(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs '%s - "%s %s HTTP/%s" %d' with the path as one of the args
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install() -> None:
    """Add the filter to uvicorn's access log, once. Called at startup, after uvicorn has set up its logging."""
    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecretPaths) for f in log.filters):
        log.addFilter(RedactSecretPaths())
