"""Device tokens: credentials for machines, replacing the shared console token (hub/devices.py, decision #59).

    python -m unittest tests.test_devices -v

Same arrangement as tests/test_users.py: a throwaway migrated database, skipped by name without Postgres.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from hub import auth, credentials  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import AuditLog, ConsoleSession, DeviceToken  # noqa: E402
from tests.test_account_login import TOKEN  # noqa: E402
from tests.test_users import UsersCase  # noqa: E402


class DevicesCase(UsersCase):
    async def issue(self, name="wall", scope="read", **extra):
        """Mint a token the way an owner does, through the API. Returns (token, device json)."""
        async with await self.sign_in_owner() as owner:
            r = await owner.post("/api/devices", json={"name": name, "scope": scope, **extra})
            self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        return body["token"], body["device"]

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    async def row(self, device_id):
        async with self.maker() as db:
            return await db.get(DeviceToken, device_id)


class Minting(DevicesCase):
    async def test_the_token_is_shown_once_and_only_its_hash_is_kept(self):
        token, device = await self.issue()
        self.assertTrue(token.startswith("njd_"))
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(device["prefix"], token[:10])
        row = await self.row(device["id"])
        self.assertEqual(row.token_hash, credentials.hash_token(token))
        self.assertNotIn(token, str(row.__dict__))
        async with await self.sign_in_owner() as owner:
            listing = (await owner.get("/api/devices")).text
        self.assertNotIn(token, listing)
        self.assertIn(device["prefix"], listing)

    async def test_the_audit_trail_records_it_and_never_the_token(self):
        token, _ = await self.issue("ci-deploys", "write", expires_days=30)
        rows = await self.audit_rows()
        self.assertIn(("device_token_created", "ci-deploys, write, 30 days", "brandon"), rows)
        self.assertNotIn(token, str(rows))
        self.assertNotIn(token[4:], str(rows))

    async def test_only_an_owner_can_make_list_or_revoke(self):
        mid = await self.make_user("max", "maintainer")
        token, device = await self.issue()
        async with await self.sign_in("max", mid) as maintainer:
            self.assertEqual((await maintainer.get("/api/devices")).status_code, 403)
            self.assertEqual((await maintainer.post("/api/devices", json={"name": "x"})).status_code, 403)
            self.assertEqual((await maintainer.delete(f"/api/devices/{device['id']}")).status_code, 403)
        async with self.client() as anon:
            for method, path in (("GET", "/api/devices"), ("POST", "/api/devices"), ("DELETE", "/api/devices/1")):
                self.assertEqual((await anon.request(method, path, json={})).status_code, 401, path)

    async def test_bad_requests_are_refused(self):
        async with await self.sign_in_owner() as owner:
            for body in ({"name": ""}, {"name": "   "}, {"name": "x" * 61}, {"name": "ok", "scope": "owner"}, {"name": "ok", "expires_days": 0}, {"name": "ok", "expires_days": 99999}):
                r = await owner.post("/api/devices", json=body)
                self.assertIn(r.status_code, (422,), body)

    async def test_the_list_says_who_made_it_when_it_was_used_and_from_where(self):
        token, device = await self.issue("wall")
        async with self.client() as c:
            await c.get("/api/errors/rising", headers={**self.bearer(token), "X-Forwarded-For": "203.0.113.9"})
        async with await self.sign_in_owner() as owner:
            (listed,) = (await owner.get("/api/devices")).json()
        self.assertEqual(listed["created_by"], "brandon")
        self.assertIsNotNone(listed["last_used_at"])
        self.assertEqual(listed["last_ip"], "203.0.113.9")


class Bearer(DevicesCase):
    async def test_a_read_device_reads_and_cannot_change_anything(self):
        token, _ = await self.issue("wall", "read")
        site = await self.seed_site()
        async with self.client() as c:
            h = self.bearer(token)
            self.assertEqual((await c.get("/api/errors/rising", headers=h)).status_code, 200)
            for method, path in (("POST", "/api/tickets"), ("PATCH", f"/api/sites/{site}")):
                r = await c.request(method, path, json={}, headers=h)
                self.assertEqual(r.status_code, 403, path)
                self.assertIn("read-only", r.text)
            s = (await c.get("/api/auth/status", headers=h)).json()
            self.assertEqual((s["role"], s["username"]), ("viewer", "device:wall"))

    async def test_a_write_device_does_the_work_but_is_never_an_owner(self):
        token, _ = await self.issue("ci", "write")
        site = await self.seed_site()
        async with self.client() as c:
            h = self.bearer(token)
            self.assertEqual((await c.patch(f"/api/sites/{site}", json={"url": "https://acme.example/h"}, headers=h)).status_code, 200)
            for method, path, body in (
                ("POST", f"/api/sites/{site}/rotate-secret", None),
                ("POST", "/api/sites", {"name": "x", "client": "Acme", "env": "production"}),
                ("GET", "/api/users", None),
                ("GET", "/api/devices", None),
                ("POST", "/api/devices", {"name": "mint-another"}),
            ):
                r = await c.request(method, path, json=body, headers=h)
                self.assertEqual(r.status_code, 403, f"{method} {path}: a leaked device token must not be able to mint more access")

    async def test_the_audit_trail_names_the_device(self):
        token, _ = await self.issue("ci", "write")
        site = await self.seed_site()
        async with self.client() as c:
            await c.patch(f"/api/sites/{site}", json={"url": "https://acme.example/h"}, headers=self.bearer(token))
            async with await self.sign_in_owner() as owner:
                rows = (await owner.get(f"/api/sites/{site}/audit")).json()
        self.assertEqual([(r["action"], r["actor"]) for r in rows], [("url_changed", "device:ci")])

    async def test_use_is_recorded_but_no_more_than_once_a_minute(self):
        token, device = await self.issue()
        async with self.client() as c:
            await c.get("/api/errors/rising", headers=self.bearer(token))
            first = (await self.row(device["id"])).last_used_at
            await c.get("/api/errors/rising", headers=self.bearer(token))
            self.assertEqual((await self.row(device["id"])).last_used_at, first)

    async def test_revoked_expired_unknown_and_malformed_tokens_are_all_401(self):
        token, device = await self.issue()
        gone, gone_device = await self.issue("old")
        async with self.maker() as db:
            row = await db.get(DeviceToken, gone_device["id"])
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        async with await self.sign_in_owner() as owner:
            self.assertEqual((await owner.delete(f"/api/devices/{device['id']}")).status_code, 200)
        async with self.client() as c:
            for presented in (token, gone, "njd_" + "x" * 43, "njd_", "njd_" + "x" * 5000, TOKEN):
                r = await c.get("/api/errors/rising", headers=self.bearer(presented))
                self.assertEqual(r.status_code, 401, presented[:12])

    async def test_the_shared_console_token_is_retired_in_account_mode(self):
        async with self.client() as c:
            self.assertEqual((await c.get("/api/errors/rising", headers=self.bearer(TOKEN))).status_code, 401)

    async def test_the_shared_console_token_still_works_in_local_development(self):
        with patch.object(settings, "operator_login", False):
            async with self.client() as c:
                self.assertEqual((await c.get("/api/errors/rising", headers=self.bearer(TOKEN))).status_code, 200)

    async def test_a_device_has_no_sessions_of_its_own_to_list(self):
        token, _ = await self.issue()
        async with self.client() as c:
            for method, path in (("GET", "/api/auth/sessions"), ("POST", "/api/auth/sessions/revoke-others")):
                self.assertEqual((await c.request(method, path, headers=self.bearer(token))).status_code, 403, path)

    async def test_the_sign_in_lock_never_reaches_a_device(self):
        token, _ = await self.issue()
        async with self.client() as c:
            for _ in range(auth.ACCOUNT_FAIL_LIMIT):
                await self.login(c, password="wrong passphrase!!")
            self.assertEqual((await self.login(c)).status_code, 429)
            self.assertEqual((await c.get("/api/errors/rising", headers=self.bearer(token))).status_code, 200)


class WallDisplay(DevicesCase):
    async def test_a_device_token_becomes_a_hardened_read_only_session(self):
        token, device = await self.issue("wall")
        async with self.client() as wall:
            r = await wall.post("/api/auth/device-login", json={"token": token})
            self.assertEqual(r.status_code, 200, r.text)
            cookie = r.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            self.assertNotIn(token, cookie, "the cookie is a new random value, not the device token")
            self.assertEqual((await wall.get("/api/errors/rising")).status_code, 200)
            s = (await wall.get("/api/auth/status")).json()
            self.assertEqual((s["role"], s["username"]), ("viewer", "device:wall"))
            self.assertEqual((await wall.post("/api/tickets", json={})).status_code, 403)
            self.assertEqual((await wall.get("/api/auth/sessions")).status_code, 403)
        (session,) = [s for s in await self.sessions() if s.device_token_id == device["id"]]
        self.assertIsNone(session.operator_id)
        self.assertEqual(session.token_hash, credentials.hash_token(r.cookies[auth.SESSION_COOKIE]))
        self.assertIn(("device_signed_in", "wall from 127.0.0.1", "device:wall"), await self.audit_rows())

    async def test_revoking_the_token_ends_the_wall_session_on_its_next_request(self):
        token, device = await self.issue("wall")
        async with self.client() as wall:
            await wall.post("/api/auth/device-login", json={"token": token})
            self.assertEqual((await wall.get("/api/errors/rising")).status_code, 200)
            async with await self.sign_in_owner() as owner:
                (listed,) = (await owner.get("/api/devices")).json()
                self.assertEqual(listed["open_sessions"], 1)
                await owner.delete(f"/api/devices/{device['id']}")
                (listed,) = (await owner.get("/api/devices")).json()
                self.assertEqual(listed["open_sessions"], 0)
            self.assertEqual((await wall.get("/api/errors/rising")).status_code, 401)

    async def test_an_expired_token_ends_a_session_it_opened_and_caps_its_length(self):
        token, device = await self.issue("wall", expires_days=1)
        async with self.client() as wall:
            await wall.post("/api/auth/device-login", json={"token": token})
            (session,) = [s for s in await self.sessions() if s.device_token_id == device["id"]]
            self.assertLessEqual(session.expires_at, (await self.row(device["id"])).expires_at)
            async with self.maker() as db:
                row = await db.get(DeviceToken, device["id"])
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
            self.assertEqual((await wall.get("/api/errors/rising")).status_code, 401)

    async def test_signing_out_ends_the_wall_session_but_not_the_token(self):
        token, _ = await self.issue("wall")
        async with self.client() as wall:
            await wall.post("/api/auth/device-login", json={"token": token})
            await wall.post("/api/auth/logout")
            self.assertEqual((await wall.get("/api/errors/rising")).status_code, 401)
            self.assertEqual((await wall.post("/api/auth/device-login", json={"token": token})).status_code, 200)

    async def test_a_wrong_token_is_401_with_no_cookie_and_guessing_is_rate_limited(self):
        token, _ = await self.issue()
        async with self.client() as wall:
            for _ in range(auth.IP_FAIL_LIMIT):
                r = await wall.post("/api/auth/device-login", json={"token": "njd_" + "z" * 43})
                self.assertEqual(r.status_code, 401)
                self.assertNotIn("set-cookie", r.headers)
            self.assertEqual((await wall.post("/api/auth/device-login", json={"token": token})).status_code, 429)

    async def test_anything_that_is_not_a_device_token_is_refused(self):
        async with self.client() as wall:
            for presented in (TOKEN, "", "njs_setup", "x" * 100):
                r = await wall.post("/api/auth/device-login", json={"token": presented})
                self.assertEqual(r.status_code, 401, presented[:10])

    async def test_it_is_not_offered_in_local_development_where_the_shared_token_signs_in(self):
        with patch.object(settings, "operator_login", False):
            async with self.client() as wall:
                self.assertEqual((await wall.post("/api/auth/device-login", json={"token": "njd_x"})).status_code, 400)


if __name__ == "__main__":
    unittest.main()
