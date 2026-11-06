"""The Caddyfile properties the hub's security depends on.

    python -m unittest tests.test_caddyfile -v

No Caddy needed: the file is read as text. This does not prove Caddy behaves, only that the lines that make it behave have
not been removed or moved somewhere that covers fewer routes.
"""

import os
import re
import unittest

CADDYFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "Caddyfile")


def lines_at_depth(text: str):
    """(depth, stripped line) for each non-comment line, depth counted in braces. The site block's own lines are depth 1."""
    depth = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        yield depth, line
        depth += line.count("{") - line.count("}")


class ForgeableAddressHeaders(unittest.TestCase):
    """hub/sources.py client_ip believes CF-Connecting-IP. With the hostname DNS-only no proxy sets it, so any copy is a forgery,
    and a forged one would let a caller dodge the sign-in throttles. It must be removed for every route."""

    def setUp(self):
        with open(CADDYFILE, encoding="utf-8") as f:
            self.lines = list(lines_at_depth(f.read()))

    def test_both_headers_are_removed_for_the_whole_site_not_one_handler(self):
        site_level = [line for depth, line in self.lines if depth == 1]
        for header in ("CF-Connecting-IP", "True-Client-IP"):
            self.assertIn(f"request_header -{header}", site_level, f"{header} must be stripped at the site level, before any handle")

    def test_no_handler_sets_either_header_back(self):
        for depth, line in self.lines:
            self.assertFalse(re.search(r"(header_up|request_header)\s+\+?(CF-Connecting-IP|True-Client-IP)\s+\S", line, re.I), line)


if __name__ == "__main__":
    unittest.main()
