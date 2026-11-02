// How the Heartbeats tab words and orders the hub's feed. Run with: npm test

import assert from "node:assert/strict";
import { badge, dueAt, every, isProblem, ordered } from "./heartbeats.ts";

const hb = (name, state, extra = {}) => ({ name, state, site: "shop", env: "production", kind: "job", interval_s: 300, last_ping_at: "2026-11-02T09:00:00Z", ...extra });

assert.equal(every(30), "every 30 s");
assert.equal(every(300), "every 5 min");
assert.equal(every(7200), "every 2 h");
assert.equal(every(86400), "every 1 d");
assert.equal(every(90), "every 90 s", "not a whole number of minutes: say seconds, never round");
console.log("  ok   a schedule is worded in its own units");

assert.deepEqual([hb("a", "ok"), hb("b", "late"), hb("c", "missed"), hb("d", "failed")].map(isProblem), [false, false, true, true]);
console.log("  ok   late is not a problem yet, missed and failed are");

assert.deepEqual(ordered([hb("z", "ok"), hb("y", "late"), hb("x", "missed"), hb("w", "failed")]).map((h) => h.name), ["w", "x", "y", "z"]);
assert.deepEqual(ordered([hb("b", "ok"), hb("a", "ok", { site: "alpha" })]).map((h) => h.name), ["a", "b"], "then by site, then name");
const input = [hb("b", "ok"), hb("a", "failed")];
ordered(input);
assert.equal(input[0].name, "b", "does not reorder the feed it was given");
console.log("  ok   problems sort first and the feed is left alone");

assert.deepEqual(badge([]), { n: 0, hot: false });
assert.deepEqual(badge([hb("a", "ok"), hb("b", "late")]), { n: 0, hot: false }, "nothing wrong, nothing to count");
assert.deepEqual(badge([hb("a", "missed")]), { n: 1, hot: false }, "a job is amber");
assert.deepEqual(badge([hb("a", "missed", { kind: "app" })]), { n: 1, hot: true }, "a production app is red");
assert.deepEqual(badge([hb("a", "missed", { kind: "app", env: "staging" })]), { n: 1, hot: false }, "staging is never red");
console.log("  ok   the badge is red only for a production app");

assert.equal(dueAt(hb("a", "ok")), Date.parse("2026-11-02T09:05:00Z"));
console.log("  ok   next ping is last ping plus the interval");
