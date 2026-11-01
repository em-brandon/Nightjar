import base64
import hashlib
import hmac

from fastapi import HTTPException

from .config import settings
from .models import Site

# A site's ingest secret is worked out from one master key held in the hub's environment, so the database holds
# nothing that can forge a report (docs/DATA-PROTECTION.md, item 4). The apps still keep a plain secret string and
# sign exactly as before, so no reporter changes. A hash would not do here, unlike a password: checking a signature
# needs the secret itself, which is why it used to be stored readable.
#
# The label is part of the derivation and is versioned. Change it and every derived secret changes with it, so it is
# fixed for good; the per-site number below is what moves when one site's secret is rotated.
LABEL = "nightjar-site-secret-v1"
# Matches the console token's floor in spirit: a short master key makes every site's secret guessable at once.
MIN_MASTER_LEN = 32


def derive(master: str, site_id: int, version: int) -> str:
    """HMAC-SHA256 of the label, site and version, keyed by the master key. 43 URL-safe characters, no padding."""
    message = f"{LABEL}|{site_id}|{version}".encode()
    digest = hmac.new(master.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def uses_derived_secret(site: Site) -> bool:
    return (site.secret_version or 0) > 0


def site_secret(site: Site) -> str:
    """The secret this site's reports are signed with: the stored one for a legacy site (version 0), otherwise derived."""
    if not uses_derived_secret(site):
        return site.ingest_secret
    if not settings.secret_master_key:
        # Never fall back to the empty stored value: signing with "" would be a secret anyone can guess. Startup
        # refuses this state (check_config); this covers a key removed while the hub is running.
        raise HTTPException(503, "this site's secret is derived, but SECRET_MASTER_KEY is not set on the hub")
    return derive(settings.secret_master_key, site.id, site.secret_version)
