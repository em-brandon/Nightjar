"""People, roles and bringing in a new account (hub/users.py, decision #59).

    python -m unittest tests.test_users -v

Same arrangement as tests/test_account_login.py: a throwaway migrated database, skipped by name without Postgres.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text  # noqa: E402

from hub import auth, credentials, users  # noqa: E402
from hub.config import settings  # noqa: E402
from hub.models import AuditLog, Client, ConsoleSession, Operator, Site  # noqa: E402
from tests.test_account_login import MASTER, PASSWORD, TOKEN, USER, AccountLogin  # noqa: E402

STRONG = "another long passphrase here"


class Signed:
    """A client that has already signed in. httpx will not open a client twice, so this hands the same one to
    `async with` and closes it afterwards."""

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        await self.client.aclose()


class UsersCase(AccountLogin):
    """AccountLogin already builds a database, an owner called brandon and a way to sign in with a code."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.engine.begin() as conn:
            await conn.execute(text("TRUNCATE sites, clients RESTART IDENTITY CASCADE"))

    async def make_user(self, username, role, password=STRONG):
        """A finished account: password set, authenticator working. Returns (id, recovery codes)."""
        codes = credentials.new_recovery_codes(2)
        async with self.maker() as db:
            op = Operator(
                username=username, password_hash=credentials.hash_password(password), role=role,
                totp_version=1, totp_last_step=0, recovery_hashes=[credentials.hash_recovery(c) for c in codes],
            )
            db.add(op)
            await db.commit()
            return op.id

    def code_for(self, op_id: int, advance=True) -> str:
        code = credentials._code(credentials.totp_secret(MASTER, op_id, 1), self.step)
        if advance:
            self.step += 1
        return code

    async def sign_in(self, username, op_id, password=STRONG):
        c = self.client()
        r = await c.post("/api/auth/login", json={"username": username, "password": password, "code": self.code_for(op_id)})
        self.assertEqual(r.status_code, 200, r.text)
        return Signed(c)

    async def sign_in_owner(self):
        c = self.client()
        r = await self.login(c)
        self.assertEqual(r.status_code, 200, r.text)
        return Signed(c)

    async def seed_site(self, name="acme-web"):
        async with self.maker() as db:
            client = Client(name="Acme")
            db.add(client)
            await db.flush()
            site = Site(client_id=client.id, name=name, env="production", url="https://acme.example")
            db.add(site)
            await db.commit()
            return site.id

    async def audit_rows(self):
        async with self.maker() as db:
            return [(a.action, a.detail, a.actor) for a in await db.scalars(select(AuditLog).order_by(AuditLog.id))]


class Roles(UsersCase):
    async def test_the_status_says_who_you_are_and_what_role_you_have(self):
        vid = await self.make_user("vera", "viewer")
        async with await self.sign_in("vera", vid) as c:
            s = (await c.get("/api/auth/status")).json()
            self.assertEqual((s["role"], s["username"]), ("viewer", "vera"))

    async def test_a_viewer_can_read_but_nothing_that_changes_anything(self):
        vid = await self.make_user("vera", "viewer")
        site = await self.seed_site()
        async with await self.sign_in("vera", vid) as c:
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 200)
            self.assertEqual((await c.get("/api/sites")).status_code, 200)
            for method, path in (("POST", "/api/tickets"), ("PATCH", f"/api/sites/{site}"), ("POST", "/api/notifications/test-push"), ("DELETE", "/api/heartbeats/1")):
                r = await c.request(method, path, json={})
                self.assertEqual(r.status_code, 403, f"{method} {path}: {r.text}")
                self.assertIn("read-only", r.text)

    async def test_a_viewer_can_still_sign_out(self):
        vid = await self.make_user("vera", "viewer")
        async with await self.sign_in("vera", vid) as c:
            self.assertEqual((await c.post("/api/auth/logout")).status_code, 200)
            self.assertEqual((await c.get("/api/errors/rising")).status_code, 401)

    async def test_a_maintainer_does_the_day_to_day_work_but_not_the_secrets(self):
        mid = await self.make_user("max", "maintainer")
        site = await self.seed_site()
        async with await self.sign_in("max", mid) as c:
            self.assertEqual((await c.patch(f"/api/sites/{site}", json={"url": "https://acme.example/health"})).status_code, 200)
            for method, path, body in (
                ("POST", "/api/sites", {"name": "x", "client": "Acme", "env": "production"}),
                ("POST", f"/api/sites/{site}/rotate-secret", None),
                ("PATCH", f"/api/sites/{site}", {"allowed_origins": ["https://acme.example"]}),
                ("GET", "/api/users", None),
                ("POST", "/api/users", {"username": "zed", "role": "viewer"}),
            ):
                r = await c.request(method, path, json=body)
                self.assertEqual(r.status_code, 403, f"{method} {path}: {r.text}")
                self.assertIn("owner", r.text)

    async def test_an_owner_can_do_all_of_it(self):
        site = await self.seed_site()
        async with await self.sign_in_owner() as c:
            self.assertEqual((await c.post(f"/api/sites/{site}/rotate-secret")).status_code, 200)
            self.assertEqual((await c.patch(f"/api/sites/{site}", json={"allowed_origins": ["https://acme.example"]})).status_code, 200)
            self.assertEqual((await c.get("/api/users")).status_code, 200)

    async def test_users_routes_are_closed_to_anyone_not_signed_in(self):
        async with self.client() as c:
            for method, path in (("GET", "/api/users"), ("POST", "/api/users"), ("PATCH", "/api/users/1"), ("POST", "/api/users/1/reset")):
                self.assertEqual((await c.request(method, path, json={})).status_code, 401, path)

    async def test_the_audit_trail_names_the_person_who_made_the_change(self):
        mid = await self.make_user("max", "maintainer")
        site = await self.seed_site()
        async with await self.sign_in("max", mid) as c:
            await c.patch(f"/api/sites/{site}", json={"url": "https://acme.example/health"})
            rows = (await c.get(f"/api/sites/{site}/audit")).json()
        self.assertEqual([(r["action"], r["actor"]) for r in rows], [("url_changed", "max")])


class Invitation(UsersCase):
    async def invite(self, c, username="newbie", role="viewer"):
        r = await c.post("/api/users", json={"username": username, "role": role})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    async def test_the_whole_journey_from_invitation_to_a_working_read_only_login(self):
        async with await self.sign_in_owner() as owner:
            issued = await self.invite(owner)
        token = issued["setup_token"]
        self.assertTrue(token.startswith("njs_"))
        self.assertTrue(issued["user"]["pending_setup"])

        async with self.client() as anon:
            # cannot sign in before setup, with any password
            r = await anon.post("/api/auth/login", json={"username": "newbie", "password": STRONG, "code": "000000"})
            self.assertEqual(r.status_code, 401)

            weak = await anon.post("/api/auth/setup/start", json={"token": token, "password": "short"})
            self.assertEqual(weak.status_code, 422)

            started = await anon.post("/api/auth/setup/start", json={"token": token, "password": STRONG})
            self.assertEqual(started.status_code, 200, started.text)
            body = started.json()
            self.assertEqual(len(body["recovery_codes"]), 8)
            self.assertIn(body["totp_secret"], body["totp_uri"])

            # a password and a secret exist, but the code has not been proven: still cannot sign in
            code = credentials._code(credentials.totp_secret(MASTER, issued["user"]["id"], 1), self.step)
            r = await anon.post("/api/auth/login", json={"username": "newbie", "password": STRONG, "code": code})
            self.assertEqual(r.status_code, 401, "sign-in stays closed until the authenticator is confirmed")

            wrong = await anon.post("/api/auth/setup/confirm", json={"token": token, "code": "000000"})
            self.assertEqual(wrong.status_code, 422)
            done = await anon.post("/api/auth/setup/confirm", json={"token": token, "code": code})
            self.assertEqual(done.status_code, 200, done.text)

            again = await anon.post("/api/auth/setup/confirm", json={"token": token, "code": code})
            self.assertEqual(again.status_code, 404, "a setup token works once")

        self.step += 1
        async with await self.sign_in("newbie", issued["user"]["id"]) as c:
            s = (await c.get("/api/auth/status")).json()
            self.assertEqual((s["role"], s["username"]), ("viewer", "newbie"))
            self.assertEqual((await c.post("/api/tickets", json={})).status_code, 403)

    async def test_the_token_is_stored_only_as_a_hash_and_is_never_listed(self):
        async with await self.sign_in_owner() as owner:
            issued = await self.invite(owner)
            listing = (await owner.get("/api/users")).text
        self.assertNotIn(issued["setup_token"], listing)
        async with self.maker() as db:
            row = await db.scalar(select(Operator).where(Operator.username == "newbie"))
        self.assertEqual(row.setup_token_hash, credentials.hash_token(issued["setup_token"]))
        self.assertEqual(row.password_hash, "")
        self.assertNotIn(issued["setup_token"], str(await self.audit_rows()))

    async def test_an_expired_or_unknown_token_is_refused_the_same_way(self):
        async with await self.sign_in_owner() as owner:
            issued = await self.invite(owner)
        async with self.maker() as db:
            row = await db.scalar(select(Operator).where(Operator.username == "newbie"))
            row.setup_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.commit()
        async with self.client() as anon:
            for token in (issued["setup_token"], "njs_nope"):
                r = await anon.post("/api/auth/setup/start", json={"token": token, "password": STRONG})
                self.assertEqual(r.status_code, 404, token)

    async def test_guessing_setup_tokens_is_rate_limited(self):
        async with self.client() as anon:
            for _ in range(auth.IP_FAIL_LIMIT):
                self.assertEqual((await anon.post("/api/auth/setup/start", json={"token": "njs_x", "password": STRONG})).status_code, 404)
            self.assertEqual((await anon.post("/api/auth/setup/start", json={"token": "njs_x", "password": STRONG})).status_code, 429)

    async def test_bad_usernames_roles_and_duplicates_are_refused(self):
        async with await self.sign_in_owner() as owner:
            for body, status in (
                ({"username": "x", "role": "viewer"}, 422),
                ({"username": "has space", "role": "viewer"}, 422),
                ({"username": "../etc", "role": "viewer"}, 422),
                ({"username": "okname", "role": "root"}, 422),
                ({"username": USER, "role": "viewer"}, 409),
                ({"username": USER.upper(), "role": "viewer"}, 409),
            ):
                self.assertEqual((await owner.post("/api/users", json=body)).status_code, status, body)

    async def test_creating_a_user_is_recorded_with_the_owner_as_actor(self):
        async with await self.sign_in_owner() as owner:
            await self.invite(owner, "newbie", "maintainer")
        self.assertIn(("user_created", "newbie as maintainer", USER), await self.audit_rows())


class Managing(UsersCase):
    async def test_a_role_change_applies_to_a_session_already_open(self):
        mid = await self.make_user("max", "maintainer")
        site = await self.seed_site()
        async with await self.sign_in("max", mid) as max_, await self.sign_in_owner() as owner:
            self.assertEqual((await max_.patch(f"/api/sites/{site}", json={"url": "https://acme.example/a"})).status_code, 200)
            r = await owner.patch(f"/api/users/{mid}", json={"role": "viewer"})
            self.assertEqual(r.json()["role"], "viewer")
            self.assertEqual((await max_.patch(f"/api/sites/{site}", json={"url": "https://acme.example/b"})).status_code, 403)
            self.assertEqual((await max_.get("/api/errors/rising")).status_code, 200)

    async def test_disabling_ends_their_sessions_and_blocks_sign_in_until_enabled(self):
        mid = await self.make_user("max", "maintainer")
        async with await self.sign_in("max", mid) as max_, await self.sign_in_owner() as owner:
            self.assertEqual((await owner.patch(f"/api/users/{mid}", json={"disabled": True})).json()["disabled"], True)
            self.assertEqual((await max_.get("/api/errors/rising")).status_code, 401)
            r = await max_.post("/api/auth/login", json={"username": "max", "password": STRONG, "code": self.code_for(mid)})
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()["detail"], "wrong username, password or code", "a disabled account answers like a wrong password")
            await owner.patch(f"/api/users/{mid}", json={"disabled": False})
            self.step += 1
            async with await self.sign_in("max", mid):
                pass
        actions = [a for a, _, _ in await self.audit_rows()]
        self.assertIn("user_disabled", actions)
        self.assertIn("user_enabled", actions)

    async def test_nobody_changes_their_own_role_or_status(self):
        async with await self.sign_in_owner() as owner:
            for body in ({"role": "viewer"}, {"disabled": True}):
                r = await owner.patch(f"/api/users/{self.op_id}", json=body)
                self.assertEqual(r.status_code, 409, body)
            self.assertEqual((await owner.post(f"/api/users/{self.op_id}/reset")).status_code, 409)

    async def test_the_last_active_owner_cannot_be_demoted_or_disabled_even_by_the_shared_token(self):
        # In local development the shared token acts as an owner with no account of its own, so it is the one way to
        # try to remove the only owner.
        with patch.object(settings, "operator_login", False):
            async with self.client() as c:
                h = {"Authorization": f"Bearer {TOKEN}"}
                for body in ({"role": "viewer"}, {"disabled": True}):
                    r = await c.patch(f"/api/users/{self.op_id}", json=body, headers=h)
                    self.assertEqual(r.status_code, 409, body)
                    self.assertIn("last active owner", r.text)
                self.assertEqual((await c.post(f"/api/users/{self.op_id}/reset", headers=h)).status_code, 409)

    async def test_with_a_second_owner_the_first_can_be_demoted(self):
        other = await self.make_user("olive", "owner")
        with patch.object(settings, "operator_login", False):
            async with self.client() as c:
                r = await c.patch(f"/api/users/{self.op_id}", json={"role": "maintainer"}, headers={"Authorization": f"Bearer {TOKEN}"})
                self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(other)

    async def test_reset_clears_the_login_ends_sessions_and_issues_a_new_token(self):
        mid = await self.make_user("max", "maintainer")
        async with await self.sign_in("max", mid) as max_, await self.sign_in_owner() as owner:
            issued = (await owner.post(f"/api/users/{mid}/reset")).json()
            self.assertEqual((await max_.get("/api/errors/rising")).status_code, 401)
            self.assertTrue(issued["user"]["pending_setup"])
            r = await max_.post("/api/auth/login", json={"username": "max", "password": STRONG, "code": self.code_for(mid)})
            self.assertEqual(r.status_code, 401, "the old password no longer works")
        async with self.maker() as db:
            row = await db.get(Operator, mid)
        self.assertEqual((row.password_hash, row.recovery_hashes, row.totp_version), ("", [], 2))
        self.assertEqual(row.setup_token_hash, credentials.hash_token(issued["setup_token"]))

    async def test_an_unknown_user_is_a_404(self):
        async with await self.sign_in_owner() as owner:
            self.assertEqual((await owner.patch("/api/users/999", json={"role": "viewer"})).status_code, 404)
            self.assertEqual((await owner.post("/api/users/999/reset")).status_code, 404)

    async def test_list_shows_state_and_never_a_secret(self):
        await self.make_user("max", "maintainer")
        async with await self.sign_in_owner() as owner:
            await owner.post("/api/users", json={"username": "newbie", "role": "viewer"})
            rows = (await owner.get("/api/users")).json()
            raw = (await owner.get("/api/users")).text
        by = {r["username"]: r for r in rows}
        self.assertEqual((by["max"]["role"], by["max"]["pending_setup"], by["max"]["disabled"]), ("maintainer", False, False))
        self.assertTrue(by["newbie"]["pending_setup"])
        for forbidden in ("password", "hash", "recovery", "totp", "njs_"):
            self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
