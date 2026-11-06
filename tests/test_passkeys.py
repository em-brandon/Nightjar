"""Passkeys (hub/passkeys.py, decision #59), tested against a software authenticator.

    python -m unittest tests.test_passkeys -v

The authenticator below is a real one in every way that matters to the server: it makes an ECDSA P-256 key, a CBOR
attestation and signed assertions in the WebAuthn wire format, so the library's verification runs for real. Nothing
is mocked. Needs the same throwaway Postgres as tests/test_users.py.
"""

import base64
import hashlib
import json
import os
import struct
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cbor2  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from sqlalchemy import select  # noqa: E402

from hub import auth, passkeys  # noqa: E402
from hub.models import ConsoleSession, Operator, Passkey  # noqa: E402
from tests.test_users import STRONG, UsersCase  # noqa: E402

ORIGIN = "http://localhost:5173"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftKey:
    """A pretend phone. `uv` is whether it checks the person (fingerprint, PIN); a server that requires user
    verification must refuse one that does not."""

    def __init__(self, uv=True, rp_id="localhost", origin=ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.uv, self.rp_id, self.origin = uv, rp_id, origin
        self.counter = 0

    def _flags(self, attested: bool) -> int:
        return 0x01 | (0x04 if self.uv else 0) | (0x40 if attested else 0)

    def _rp_hash(self) -> bytes:
        return hashlib.sha256(self.rp_id.encode()).digest()

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def register(self, options: dict) -> dict:
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth_data = self._rp_hash() + bytes([self._flags(True)]) + struct.pack(">I", self.counter) + bytes(16) \
            + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose
        return {
            "id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key", "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": b64(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})),
                "transports": ["internal"],
            },
        }

    def assert_(self, options: dict, counter=None, user_handle=b"1") -> dict:
        self.counter = self.counter + 1 if counter is None else counter
        auth_data = self._rp_hash() + bytes([self._flags(False)]) + struct.pack(">I", self.counter)
        client = self._client_data("webauthn.get", options["challenge"])
        sig = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key", "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64(client), "authenticatorData": b64(auth_data), "signature": b64(sig),
                "userHandle": b64(user_handle),
            },
        }


class PasskeyCase(UsersCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        passkeys._challenges.clear()

    async def add_passkey(self, c, key=None, name="my phone", password=None):
        """Register through the API the way the console does. Returns (key, response)."""
        key = key or SoftKey()
        opts = (await c.post("/api/passkeys/register/options")).json()
        r = await c.post("/api/passkeys/register", json={
            "challenge_id": opts["challenge_id"], "name": name, "password": password or self.password,
            "credential": key.register(opts["options"]),
        })
        return key, r

    @property
    def password(self):
        from tests.test_account_login import PASSWORD
        return PASSWORD

    async def passkey_login(self, c, key, **over):
        opts = (await c.post("/api/auth/passkey/options")).json()
        body = {"challenge_id": opts["challenge_id"], "credential": key.assert_(opts["options"])}
        body.update(over)
        return await c.post("/api/auth/passkey/login", json=body)

    async def registered(self, name="my phone"):
        """An owner with one working passkey. Returns the key."""
        async with await self.sign_in_owner() as c:
            key, r = await self.add_passkey(c, name=name)
            self.assertEqual(r.status_code, 201, r.text)
        return key


class Registering(PasskeyCase):
    async def test_a_signed_in_person_registers_a_passkey_and_only_the_public_key_is_kept(self):
        async with await self.sign_in_owner() as c:
            key, r = await self.add_passkey(c)
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.json()["name"], "my phone")
            listing = (await c.get("/api/passkeys")).json()
            self.assertEqual([p["name"] for p in listing], ["my phone"])
            self.assertEqual(set(listing[0]), {"id", "name", "created_at", "last_used_at"}, "nothing secret or identifying leaves the hub")
        async with self.maker() as db:
            row = await db.scalar(select(Passkey))
        self.assertEqual(bytes(row.credential_id), key.cred_id)
        self.assertNotIn(key.key.private_numbers().private_value.to_bytes(32, "big"), bytes(row.public_key))
        self.assertIn(("passkey_added", "my phone", "brandon"), await self.audit_rows())

    async def test_options_ask_for_a_discoverable_credential_with_user_verification(self):
        async with await self.sign_in_owner() as c:
            o = (await c.post("/api/passkeys/register/options")).json()["options"]
        self.assertEqual(o["rp"]["id"], "localhost")
        self.assertEqual(o["authenticatorSelection"]["residentKey"], "required")
        self.assertEqual(o["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(o["user"]["name"], "brandon")

    async def test_the_password_is_asked_for_again_and_a_wrong_one_is_counted_and_audited(self):
        async with await self.sign_in_owner() as c:
            key, r = await self.add_passkey(c, password="not the password at all")
            self.assertEqual(r.status_code, 401)
            async with self.maker() as db:
                self.assertEqual(await db.scalar(select(Passkey.id)), None)
            for _ in range(auth.ACCOUNT_FAIL_LIMIT):
                key, r = await self.add_passkey(c, password="not the password at all")
            self.assertEqual(r.status_code, 429, "a stolen cookie cannot guess the password through this route")
        self.assertIn("password_check_failed", [a for a, _, _ in await self.audit_rows()])

    async def test_a_challenge_works_once_and_only_for_what_it_was_made_for(self):
        async with await self.sign_in_owner() as c:
            key = SoftKey()
            opts = (await c.post("/api/passkeys/register/options")).json()
            body = {"challenge_id": opts["challenge_id"], "name": "x", "password": self.password, "credential": key.register(opts["options"])}
            self.assertEqual((await c.post("/api/passkeys/register", json=body)).status_code, 201)
            self.assertEqual((await c.post("/api/passkeys/register", json=body)).status_code, 400, "replayed")
            login_opts = (await self.client().post("/api/auth/passkey/options")).json()
            body["challenge_id"] = login_opts["challenge_id"]
            body["credential"] = SoftKey().register(login_opts["options"])
            self.assertEqual((await c.post("/api/passkeys/register", json=body)).status_code, 400, "a sign-in challenge is not a registration one")

    async def test_an_expired_challenge_is_refused(self):
        async with await self.sign_in_owner() as c:
            key = SoftKey()
            opts = (await c.post("/api/passkeys/register/options")).json()
            challenge, purpose, op, _ = passkeys._challenges[opts["challenge_id"]]
            passkeys._challenges[opts["challenge_id"]] = (challenge, purpose, op, time.monotonic() - 1)
            r = await c.post("/api/passkeys/register", json={"challenge_id": opts["challenge_id"], "name": "x", "password": self.password, "credential": key.register(opts["options"])})
            self.assertEqual(r.status_code, 400)

    async def test_the_wrong_site_or_origin_or_an_authenticator_that_skips_the_check_is_refused(self):
        async with await self.sign_in_owner() as c:
            for bad in (SoftKey(origin="https://evil.example"), SoftKey(rp_id="evil.example"), SoftKey(uv=False)):
                _, r = await self.add_passkey(c, key=bad)
                self.assertEqual(r.status_code, 400, (bad.origin, bad.rp_id, bad.uv))
            self.assertEqual((await c.get("/api/passkeys")).json(), [])

    async def test_garbage_is_a_400_not_a_crash(self):
        async with await self.sign_in_owner() as c:
            opts = (await c.post("/api/passkeys/register/options")).json()
            r = await c.post("/api/passkeys/register", json={"challenge_id": opts["challenge_id"], "name": "x", "password": self.password, "credential": {"nonsense": 1}})
            self.assertIn(r.status_code, (400, 422))

    async def test_the_same_authenticator_cannot_be_added_twice_and_the_list_is_capped(self):
        async with await self.sign_in_owner() as c:
            key, r = await self.add_passkey(c)
            self.assertEqual(r.status_code, 201)
            _, again = await self.add_passkey(c, key=key)
            self.assertEqual(again.status_code, 409, "the options exclude it, and a forged repeat is refused too")
            for i in range(passkeys.MAX_PER_OPERATOR - 1):
                _, r = await self.add_passkey(c, name=f"k{i}")
                self.assertEqual(r.status_code, 201)
            self.assertEqual((await c.post("/api/passkeys/register/options")).status_code, 409)

    async def test_any_role_can_have_one_but_a_device_and_a_stranger_cannot(self):
        vid = await self.make_user("vera", "viewer")
        async with await self.sign_in("vera", vid) as c:
            _, r = await self.add_passkey(c, password=STRONG)
            self.assertEqual(r.status_code, 201)
        async with self.client() as anon:
            self.assertEqual((await anon.post("/api/passkeys/register/options")).status_code, 401)
            self.assertEqual((await anon.get("/api/passkeys")).status_code, 401)

    async def test_you_see_and_remove_only_your_own(self):
        vid = await self.make_user("vera", "viewer")
        await self.registered("owners phone")
        async with await self.sign_in("vera", vid) as v:
            await self.add_passkey(v, password=STRONG, name="veras phone")
            self.assertEqual([p["name"] for p in (await v.get("/api/passkeys")).json()], ["veras phone"])
            async with self.maker() as db:
                owners = await db.scalar(select(Passkey.id).where(Passkey.name == "owners phone"))
            self.assertEqual((await v.delete(f"/api/passkeys/{owners}")).status_code, 404)
        async with await self.sign_in_owner() as o:
            self.assertEqual((await o.delete(f"/api/passkeys/{owners}")).status_code, 200)
            self.assertEqual((await o.get("/api/passkeys")).json(), [])
        self.assertIn(("passkey_removed", "owners phone", "brandon"), await self.audit_rows())


class SigningIn(PasskeyCase):
    async def test_a_passkey_signs_in_with_no_username_password_or_code(self):
        key = await self.registered()
        async with self.client() as phone:
            r = await self.passkey_login(phone, key)
            self.assertEqual(r.status_code, 200, r.text)
            cookie = r.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            s = (await phone.get("/api/auth/status")).json()
            self.assertEqual((s["role"], s["username"]), ("owner", "brandon"))
            self.assertEqual((await phone.get("/api/errors/rising")).status_code, 200)
        self.assertTrue(any(a == "login" and "passkey my phone" in d for a, d, _ in await self.audit_rows()))
        async with self.maker() as db:
            self.assertIsNotNone((await db.scalar(select(Passkey))).last_used_at)

    async def test_the_session_is_an_ordinary_revocable_one(self):
        key = await self.registered()
        async with self.client() as phone:
            await self.passkey_login(phone, key)
            self.assertEqual(len((await phone.get("/api/auth/sessions")).json()), 2, "the one that registered it, and this one")
            await phone.post("/api/auth/logout")
            self.assertEqual((await phone.get("/api/errors/rising")).status_code, 401)

    async def test_the_options_name_no_credentials_so_no_username_is_revealed(self):
        async with self.client() as c:
            o = (await c.post("/api/auth/passkey/options")).json()["options"]
        self.assertFalse(o.get("allowCredentials"))
        self.assertEqual(o["userVerification"], "required")

    async def test_a_signature_cannot_be_replayed(self):
        key = await self.registered()
        async with self.client() as phone:
            opts = (await phone.post("/api/auth/passkey/options")).json()
            body = {"challenge_id": opts["challenge_id"], "credential": key.assert_(opts["options"])}
            self.assertEqual((await phone.post("/api/auth/passkey/login", json=body)).status_code, 200)
            self.assertEqual((await phone.post("/api/auth/passkey/login", json=body)).status_code, 400)

    async def test_a_counter_that_goes_backwards_is_refused_as_a_cloned_key(self):
        key = await self.registered()
        async with self.client() as phone:
            opts = (await phone.post("/api/auth/passkey/options")).json()
            ok = {"challenge_id": opts["challenge_id"], "credential": key.assert_(opts["options"], counter=10)}
            self.assertEqual((await phone.post("/api/auth/passkey/login", json=ok)).status_code, 200)
            opts = (await phone.post("/api/auth/passkey/options")).json()
            back = {"challenge_id": opts["challenge_id"], "credential": key.assert_(opts["options"], counter=3)}
            self.assertEqual((await phone.post("/api/auth/passkey/login", json=back)).status_code, 401)

    async def test_a_key_the_hub_never_saw_a_forged_signature_and_the_wrong_site_all_fail_the_same_way(self):
        key = await self.registered()
        stranger = SoftKey()
        stranger.cred_id = key.cred_id  # same id, a different private key
        phishing = SoftKey(origin="https://evil.example")
        phishing.key, phishing.cred_id = key.key, key.cred_id
        async with self.client() as phone:
            for impostor in (SoftKey(), stranger, phishing):
                r = await self.passkey_login(phone, impostor)
                self.assertEqual(r.status_code, 401, (impostor.origin,))
                self.assertEqual(r.json(), {"detail": "that passkey was not accepted"})
                self.assertNotIn("set-cookie", r.headers)

    async def test_a_disabled_account_cannot_use_its_passkey(self):
        key = await self.registered()
        async with self.maker() as db:
            (await db.scalar(select(Operator))).disabled_at = datetime.now(timezone.utc)
            await db.commit()
        async with self.client() as phone:
            r = await self.passkey_login(phone, key)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json(), {"detail": "that passkey was not accepted"})

    async def test_disabling_an_account_ends_nothing_new_but_deleting_it_removes_its_passkeys(self):
        await self.registered()
        async with self.maker() as db:
            op = await db.scalar(select(Operator))
            await db.delete(op)
            await db.commit()
            self.assertIsNone(await db.scalar(select(Passkey.id)))

    async def test_guessing_is_rate_limited_by_address(self):
        key = await self.registered()
        async with self.client() as phone:
            for _ in range(auth.IP_FAIL_LIMIT):
                await self.passkey_login(phone, SoftKey())
            self.assertEqual((await self.passkey_login(phone, key)).status_code, 429)

    async def test_garbage_and_an_unknown_challenge_are_refused_cleanly(self):
        async with self.client() as phone:
            for body in ({"challenge_id": "nope", "credential": {}}, {"challenge_id": "x" * 500, "credential": {}}):
                self.assertIn((await phone.post("/api/auth/passkey/login", json=body)).status_code, (400, 422))
            opts = (await phone.post("/api/auth/passkey/options")).json()
            r = await phone.post("/api/auth/passkey/login", json={"challenge_id": opts["challenge_id"], "credential": {"rawId": 5}})
            self.assertEqual(r.status_code, 401)

    async def test_the_password_and_code_still_work_after_a_passkey_is_added(self):
        await self.registered()
        async with self.client() as c:
            self.assertEqual((await self.login(c)).status_code, 200)

    async def test_the_challenge_store_cannot_grow_without_bound(self):
        async with self.client() as c:
            for _ in range(passkeys.MAX_CHALLENGES + 20):
                await c.post("/api/auth/passkey/options")
        self.assertLessEqual(len(passkeys._challenges), passkeys.MAX_CHALLENGES)


class Configuration(unittest.TestCase):
    def test_development_defaults_and_derivation_from_the_public_url(self):
        from unittest.mock import patch
        from hub.config import settings
        with patch.object(settings, "webauthn_rp_id", ""), patch.object(settings, "webauthn_origins", ""), patch.object(settings, "hub_public_url", ""):
            self.assertEqual(passkeys.rp_id(), "localhost")
            self.assertIn("http://localhost:5173", passkeys.origins())
        with patch.object(settings, "webauthn_rp_id", ""), patch.object(settings, "webauthn_origins", ""), patch.object(settings, "hub_public_url", "https://hub.example.com/"):
            self.assertEqual(passkeys.rp_id(), "hub.example.com")
            self.assertEqual(passkeys.origins(), ["https://hub.example.com"])
        with patch.object(settings, "webauthn_rp_id", "example.com"), patch.object(settings, "webauthn_origins", "https://a.example.com/, https://b.example.com"):
            self.assertEqual(passkeys.rp_id(), "example.com")
            self.assertEqual(passkeys.origins(), ["https://a.example.com", "https://b.example.com"])


if __name__ == "__main__":
    unittest.main()
