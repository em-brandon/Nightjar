"""Keeps the uptime probe on the public internet (security audit finding 2).

The probe fetches a site's URL from the hub's own network position. Without a check, anyone who can set a URL (a maintainer, or a
stolen maintainer session) could point it at the database, the hub itself, a cloud metadata address or anything else behind the
firewall, and learn from the up/down result and latency what is reachable. A client's own site could also redirect it inward.

So the probe refuses an address that is not globally routable, on the first request and on every redirect. Three honest limits:

  * the name is resolved here and again by the HTTP client, so a DNS answer that changes between the two (rebinding) can still
    get through. Closing that means connecting to the checked address, which the client library does not offer without
    rewriting the request; the other layers (no secrets in responses, nothing returned to the caller) bound the damage
  * a name that does not resolve is let through to fail in the ordinary way, so a site that is down is still reported as down
  * a private network you really do monitor (a Tailscale address, say) is allowed on purpose with PROBE_ALLOWED_NETWORKS
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from .config import settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for part in settings.probe_allowed_networks.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    if not settings.verify_signatures:
        # local development: a site on your own machine is the normal case
        nets += [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
    return nets


def _blocked(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 is 10.0.0.1
    if any(ip in net for net in _allowed_networks() if net.version == ip.version):
        return False
    return not ip.is_global or ip.is_multicast


def literal_problem(url: str) -> str | None:
    """A problem visible in the URL alone, with no name lookup: an IP address that is not public, or localhost. Used when a URL is
    saved, so the mistake is caught at the form and not as a failed check later."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return "the address has no host"
    if host == "localhost" or host.endswith(".localhost"):
        if not any(ipaddress.ip_address("127.0.0.1") in n for n in _allowed_networks()):
            return "localhost is not a public address"
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # a name: looked up when it is probed
    return "that is not a public address" if _blocked(ip) else None


async def problem(url: str) -> str | None:
    """Why this URL must not be probed, or None. Looks the name up, and refuses if ANY answer is not public."""
    if (reason := literal_problem(url)) is not None:
        return reason
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return None  # a literal that passed above
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None  # does not resolve: it will fail as an ordinary outage
    for info in infos:
        try:
            if _blocked(ipaddress.ip_address(info[4][0].split("%")[0])):
                return "the name resolves to a non-public address"
        except ValueError:
            continue
    return None
