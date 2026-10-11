// The onboarding snippets: values filled in, the secret only where it belongs. Run with: npm test

import assert from "node:assert/strict";
import { STACKS, SECRET_PLACEHOLDER, PUBLIC_KEY_PLACEHOLDER, envBlock, snippet } from "./snippets.ts";

const base = { hub: "https://nightjar.example.co.ke/", ingestKey: "KEY123", ingestSecret: "SECRET456", publicKey: "PUB789" };

for (const { id } of STACKS) {
  const s = snippet({ ...base, stack: id });
  assert.ok(s.code.length > 0 && s.filename, `${id} has code and a filename`);
  assert.ok(!s.code.includes("SECRET456"), `${id}: the secret is never written into the code`);
  assert.ok(!/\/\/ingest|\.ke\/\//.test(s.code), `${id}: a trailing slash on the hub does not double up`);
}
console.log("  ok   every stack has code, and none puts the secret in it");

assert.match(snippet({ ...base, stack: "python" }).code, /https:\/\/nightjar\.example\.co\.ke\/ingest\/KEY123\/event/);
assert.match(snippet({ ...base, stack: "php" }).code, /'key'\s+=> 'KEY123'/);
assert.match(snippet({ ...base, stack: "php" }).code, /'hub'\s+=> 'https:\/\/nightjar\.example\.co\.ke'/);
console.log("  ok   the key and hub are filled in");

const e = snippet({ ...base, stack: "node" }).env;
assert.deepEqual(e.split("\n"), ["NIGHTJAR_URL=https://nightjar.example.co.ke", "NIGHTJAR_KEY=KEY123", "NIGHTJAR_SECRET=SECRET456"]);
assert.match(envBlock(base.hub, "K"), new RegExp(`NIGHTJAR_SECRET=${SECRET_PLACEHOLDER}`), "no secret known: a placeholder, never blank");
assert.match(envBlock(base.hub, "K", null), new RegExp(SECRET_PLACEHOLDER));
console.log("  ok   the environment block has the secret once, or a placeholder");

const b = snippet({ ...base, stack: "browser" });
assert.equal(b.env, "", "a page is never given the ingest secret or key");
assert.match(b.code, /key: "PUB789"/);
assert.ok(!b.code.includes("KEY123") && !b.code.includes("SECRET456"));
const off = snippet({ ...base, publicKey: null, stack: "browser" });
assert.match(off.code, new RegExp(PUBLIC_KEY_PLACEHOLDER));
assert.match(off.notes[0], /off/i, "browser reporting being off is said first");
console.log("  ok   the browser snippet uses the public key only, and says when it is off");

console.log("snippet rules passed");
