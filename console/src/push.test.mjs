// The staging push card's rules. Run with: npm test

import assert from "node:assert/strict";
import {
  COMPLETE_SHOW_MIN, DEMO_COMPLETE_S, DEMO_DONE_S, DEMO_GAPS_S, DEMO_LOADING_S, DEMO_PERIOD_S,
  DONE_SHOW_MIN, FAILED_SHOW_MIN, RUNNING_MAX_MIN, demoPush, pushView, stackHealthySince,
} from "./push.ts";

const ok = (name) => console.log(`  ok   ${name}`);
const NOW = Date.parse("2026-09-21T12:00:00Z");
const ago = (min) => new Date(NOW - min * 60_000).toISOString();
const d = (status, min, over = {}) => ({ env: "staging", site: "shop-staging", ref: "develop@abc", status, created_at: ago(min), progress: null, ...over });
const stack = (over = {}) => ({ site: "shop-staging", env: "staging", state: "running", stale: false, reported_at: ago(0), ...over });

assert.equal(pushView([], NOW), null, "no deploys, no card");
assert.equal(pushView([{ ...d("running", 1), env: "production" }], NOW), null, "production pushes are not this card");
ok("nothing to show");

const loading = pushView([d("running", 2, { progress: 40 })], NOW);
assert.deepEqual([loading.state, loading.progress, loading.site, loading.ref], ["loading", 40, "shop-staging", "develop@abc"]);
assert.equal(pushView([d("running", 2)], NOW).progress, null, "no progress reported is an indeterminate bar, not 0%");
assert.equal(pushView([d("running", RUNNING_MAX_MIN + 1)], NOW), null, "a CI job that never finished is not still going");
ok("loading, with and without progress");

// done is not complete: the upload finished, but nothing has proved the stack is up
assert.equal(pushView([d("success", 1)], NOW, []).state, "done");
assert.equal(pushView([d("success", 1)], NOW, [stack({ reported_at: ago(5) })]).state, "done", "a report from BEFORE the deploy proves nothing");
assert.equal(pushView([d("success", 1)], NOW, [stack({ stale: true })]).state, "done", "a silent reporter proves nothing");
assert.equal(pushView([d("success", 1)], NOW, [stack({ state: "degraded" })]).state, "done", "a degraded stack is not complete");
assert.equal(pushView([d("success", 1)], NOW, [stack({ site: "other-staging" })]).state, "done", "another site's stack is not this one's");
assert.equal(pushView([d("success", DONE_SHOW_MIN + 1)], NOW, []), null, "done fades if nothing confirms it");
ok("done is not complete");

const complete = pushView([d("success", 2)], NOW, [stack()]);
assert.deepEqual([complete.state, complete.progress], ["complete", 100]);
assert.equal(pushView([d("success", COMPLETE_SHOW_MIN + 1)], NOW, [stack()]), null, "complete goes after its own time");
assert.ok(COMPLETE_SHOW_MIN < DONE_SHOW_MIN, "a confirmed push needs less time on screen than an unconfirmed one");
assert.equal(stackHealthySince([stack({ reported_at: ago(1) })], "shop-staging", NOW - 2 * 60_000), true);
assert.equal(stackHealthySince([], "shop-staging", 0), false);
ok("complete needs the staging stack to have come back healthy");

assert.equal(pushView([d("failed", 30)], NOW, [stack()]).state, "failed", "a healthy older stack does not hide a failed push");
assert.equal(pushView([d("failed", FAILED_SHOW_MIN + 1)], NOW), null);
assert.ok(FAILED_SHOW_MIN > DONE_SHOW_MIN, "a failure stays longer than a success");
ok("failed stays longest");

assert.equal(pushView([d("success", 1), d("running", 3)], NOW).state, "done", "the newest staging deploy wins");
assert.equal(pushView([{ ...d("running", 1), env: "production" }, d("failed", 5)], NOW).state, "failed", "the newest STAGING one");
ok("the newest staging deploy wins");

// ---- the sample: every state for its own time, then away, then back after 40 and 30 second pauses
const at = (sec) => demoPush(sec * 1000);
const SHOWN = DEMO_LOADING_S + DEMO_DONE_S + DEMO_COMPLETE_S;
assert.deepEqual(DEMO_GAPS_S, [40, 30]);
assert.deepEqual([at(0).state, at(0).progress], ["loading", 0]);
assert.deepEqual([at(6).state, at(6).progress], ["loading", 50]);
assert.equal(at(DEMO_LOADING_S).state, "done");
assert.equal(at(DEMO_LOADING_S + DEMO_DONE_S - 0.1).state, "done");
assert.equal(at(DEMO_LOADING_S + DEMO_DONE_S).state, "complete");
assert.equal(at(SHOWN - 0.1).state, "complete");
assert.equal(at(SHOWN), null, "then the card goes away");
assert.equal(at(SHOWN + 39.9), null, "and stays away for 40 seconds");
assert.equal(at(SHOWN + 40).state, "loading", "then it comes back");
const second = SHOWN + 40; // the second showing starts here
assert.equal(at(second + SHOWN), null);
assert.equal(at(second + SHOWN + 29.9), null, "the next pause is 30 seconds");
assert.equal(at(second + SHOWN + 30).state, "loading");
assert.equal(DEMO_PERIOD_S, 2 * SHOWN + 70);
assert.equal(at(DEMO_PERIOD_S + 6).progress, 50, "and the whole rhythm repeats");
ok("the sample walks through every state, goes away, and returns after 40 then 30 seconds");

// how much of the time it is on screen, so it never becomes wallpaper
let shown = 0;
for (let s = 0; s < DEMO_PERIOD_S; s += 0.5) if (at(s)) shown += 0.5;
assert.equal(shown, 2 * SHOWN);
ok("the sample is on screen for less than half the time");

console.log("\npush rules passed");
