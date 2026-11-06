"""The probe may not be pointed at the hub's own network (hub/netguard.py, security audit finding 2).

    python -m unittest tests.test_netguard -v

No database. HTTP is faked with httpx's mock transport; name lookups are faked by replacing socket.getaddrinfo.
"""

import os
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from hub import netguard, probe  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.schemas import SiteIn, SitePatch  # noqa: E402

PUBLIC = "http://93.184.216.34/"


def answers(*addresses):
    """A fake socket.getaddrinfo that returns these addresses for any name."""
    def fake(host, port, *a, **kw):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in addresses]
    return patch.object(socket, "getaddrinfo", fake)


class Production(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (patch.object(settings, "verify_signatures", True), patch.object(settings, "probe_allowed_networks", "")):
            p.start()
            self.addCleanup(p.stop)

    def test_addresses_inside_the_network_are_refused_as_literals(self):
        for host in ("127.0.0.1", "10.0.0.5", "172.16.3.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                     "[::1]", "[fe80::1]", "[fd00::1]", "[::ffff:10.0.0.1]", "[::ffff:169.254.169.254]", "224.0.0.1", "localhost", "app.localhost"):
            self.assertIsNotNone(netguard.literal_problem(f"http://{host}/health"), host)

    def test_public_addresses_and_names_pass_the_literal_check(self):
        for url in (PUBLIC, "https://example.com/h", "http://[2606:4700::1111]/"):
            self.assertIsNone(netguard.literal_problem(url), url)

    async def test_a_name_that_resolves_inward_is_refused_even_if_one_answer_is_public(self):
        with answers("93.184.216.34", "10.0.0.7"):
            self.assertIn("non-public", await netguard.problem("https://trick.example/"))
        with answers("169.254.169.254"):
            self.assertIsNotNone(await netguard.problem("https://metadata.example/"))
        with answers("93.184.216.34"):
            self.assertIsNone(await netguard.problem("https://fine.example/"))

    async def test_a_name_that_does_not_resolve_is_left_to_fail_as_an_outage(self):
        def nope(*a, **kw):
            raise socket.gaierror("no such host")
        with patch.object(socket, "getaddrinfo", nope):
            self.assertIsNone(await netguard.problem("https://gone.example/"))

    def test_a_monitored_private_network_can_be_allowed_on_purpose(self):
        with patch.object(settings, "probe_allowed_networks", "100.64.0.0/10, 10.1.0.0/16"):
            self.assertIsNone(netguard.literal_problem("http://100.100.1.1/"))
            self.assertIsNone(netguard.literal_problem("http://10.1.2.3/"))
            self.assertIsNotNone(netguard.literal_problem("http://10.2.0.1/"), "only the listed ranges")
            self.assertIsNotNone(netguard.literal_problem("http://169.254.169.254/"))

    def test_the_site_form_refuses_such_a_url(self):
        with self.assertRaises(ValidationError):
            SiteIn(name="acme", client="Acme", url="http://169.254.169.254/latest/meta-data/")
        with self.assertRaises(ValidationError):
            SitePatch(url="http://127.0.0.1:5432/")
        self.assertEqual(SitePatch(url="https://acme.example/health").url, "https://acme.example/health")


class Development(unittest.TestCase):
    def test_loopback_is_fine_on_your_own_machine_when_signatures_are_off(self):
        with patch.object(settings, "verify_signatures", False), patch.object(settings, "probe_allowed_networks", ""):
            self.assertIsNone(netguard.literal_problem("http://localhost:3000/"))
            self.assertIsNone(netguard.literal_problem("http://127.0.0.1:3000/"))
            self.assertIsNotNone(netguard.literal_problem("http://169.254.169.254/"), "metadata is never fine")
            self.assertIsNotNone(netguard.literal_problem("http://10.0.0.5/"))


class Probing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for p in (patch.object(settings, "verify_signatures", True), patch.object(settings, "probe_allowed_networks", "")):
            p.start()
            self.addCleanup(p.stop)

    def client(self, handler):
        self.seen = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.seen.append(str(request.url))
            return handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(recording), follow_redirects=False)

    async def test_an_ordinary_site_is_checked(self):
        async with self.client(lambda r: httpx.Response(200)) as c:
            check = await probe._probe(c, 1, PUBLIC)
        self.assertTrue(check.ok)
        self.assertEqual(self.seen, [PUBLIC])

    async def test_an_inward_url_is_never_requested(self):
        async with self.client(lambda r: httpx.Response(200)) as c:
            check = await probe._probe(c, 1, "http://169.254.169.254/latest/meta-data/")
        self.assertFalse(check.ok)
        self.assertTrue(check.error.startswith("blocked:"), check.error)
        self.assertEqual(self.seen, [], "not one packet was sent")

    async def test_a_redirect_to_the_inside_is_not_followed(self):
        def handler(request):
            if request.url.host == "93.184.216.34":
                return httpx.Response(302, headers={"location": "http://10.0.0.5:5432/"})
            return httpx.Response(200)

        async with self.client(handler) as c:
            check = await probe._probe(c, 1, PUBLIC)
        self.assertFalse(check.ok)
        self.assertIn("blocked", check.error)
        self.assertEqual(self.seen, [PUBLIC], "the public hop was fetched, the inward one never")

    async def test_a_relative_redirect_is_followed_to_the_end(self):
        def handler(request):
            return httpx.Response(301, headers={"location": "/home"}) if request.url.path == "/" else httpx.Response(200)

        async with self.client(handler) as c:
            check = await probe._probe(c, 1, PUBLIC)
        self.assertTrue(check.ok)
        self.assertEqual(self.seen, [PUBLIC, "http://93.184.216.34/home"])

    async def test_a_redirect_loop_and_a_non_web_redirect_stop(self):
        async with self.client(lambda r: httpx.Response(302, headers={"location": "/again"})) as c:
            check = await probe._probe(c, 1, PUBLIC)
        self.assertIn("too many redirects", check.error)
        self.assertEqual(len(self.seen), probe.MAX_REDIRECTS + 1)
        async with self.client(lambda r: httpx.Response(302, headers={"location": "file:///etc/passwd"})) as c:
            check = await probe._probe(c, 1, PUBLIC)
        self.assertIn("blocked", check.error)

    async def test_a_down_site_is_still_a_down_site(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        async with self.client(boom) as c:
            check = await probe._probe(c, 1, PUBLIC)
        self.assertFalse(check.ok)
        self.assertEqual(check.error, "ConnectError")


if __name__ == "__main__":
    unittest.main()
