// The conversions between the hub's JSON and the browser's WebAuthn types. Run with: npm test

import assert from "node:assert/strict";
import { b64urlToBytes, bytesToB64url, creationOptions, requestOptions } from "./passkey.ts";

// every length, because padding is where base64 conversions go wrong
for (let n = 0; n < 40; n++) {
  const bytes = Uint8Array.from({ length: n }, (_, i) => (i * 37 + 250) % 256);
  const text = bytesToB64url(bytes);
  assert.match(text, /^[A-Za-z0-9_-]*$/, "url-safe, no padding");
  assert.deepEqual(Array.from(b64urlToBytes(text)), Array.from(bytes), `length ${n}`);
}
assert.equal(bytesToB64url(Uint8Array.from([0xfb, 0xff, 0xfe])), "-__-", "uses - and _ where base64 uses + and /");
assert.equal(bytesToB64url(new Uint8Array([1, 2, 3]).buffer), "AQID", "an ArrayBuffer works too");

const wire = {
  challenge: "AQID",
  rp: { id: "hub.example.com", name: "Nightjar" },
  user: { id: "MQ", name: "brandon", displayName: "brandon" },
  pubKeyCredParams: [{ type: "public-key", alg: -7 }],
  excludeCredentials: [{ id: "BAUG", type: "public-key" }],
  authenticatorSelection: { residentKey: "required", userVerification: "required" },
};
const made = creationOptions(wire);
assert.deepEqual(Array.from(new Uint8Array(made.challenge)), [1, 2, 3]);
assert.deepEqual(Array.from(new Uint8Array(made.user.id)), [49], "the user handle is the id as bytes");
assert.deepEqual(Array.from(new Uint8Array(made.excludeCredentials[0].id)), [4, 5, 6]);
assert.equal(made.authenticatorSelection.residentKey, "required", "everything else passes through untouched");
assert.throws(() => creationOptions({ challenge: "AQID" }), /incomplete/);

const asked = requestOptions({ challenge: "AQID", rpId: "hub.example.com", userVerification: "required" });
assert.deepEqual(Array.from(new Uint8Array(asked.challenge)), [1, 2, 3]);
assert.deepEqual(asked.allowCredentials, [], "no username: the authenticator offers its own passkeys");
console.log("passkey: ok");
