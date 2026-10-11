// What the console says about alerts that did not reach the phone. Run with: npm test

import assert from "node:assert/strict";
import { alertNotice } from "./alerts.ts";

const alert = (over) => ({ id: 1, site: "acme-shop", status: "failed", last_error: "Pushover timed out", created_at: "2026-10-12T09:00:00Z", ...over });

assert.equal(alertNotice([]), null, "nothing wrong says nothing");
console.log("  ok   no undelivered alerts, no notice");

const one = alertNotice([alert()]);
assert.equal(one.tone, "bad");
assert.equal(one.text, "1 alert could not be sent to your phone");
assert.equal(one.detail, "acme-shop: Pushover timed out");
assert.match(alertNotice([alert(), alert({ id: 2 })]).text, /^2 alerts could not be sent/);
console.log("  ok   a failed alert is red, counted, and says why");

const retry = alertNotice([alert({ status: "pending", last_error: "Pushover answered 503" })]);
assert.equal(retry.tone, "warn", "still trying is a warning, not red");
assert.equal(retry.text, "1 alert is being retried after a failed send");
assert.equal(alertNotice([alert({ status: "sending" }), alert({ id: 2, status: "pending" })]).text, "2 alerts are being retried after a failed send");
console.log("  ok   an alert still being retried is a warning");

const mixed = alertNotice([alert({ status: "pending", id: 2 }), alert()]);
assert.equal(mixed.tone, "bad", "one given up is enough to be red");
assert.match(mixed.text, /1 alert could not be sent to your phone · 1 more being retried/);
console.log("  ok   failed and retrying together are red and counted separately");

// the detail is about the newest failed alert, not an old one
const detail = alertNotice([
  alert({ id: 1, site: "old-site", last_error: "stale reason", created_at: "2026-10-01T00:00:00Z" }),
  alert({ id: 2, site: "new-site", last_error: "fresh reason", created_at: "2026-10-12T09:00:00Z" }),
]).detail;
assert.equal(detail, "new-site: fresh reason");
assert.equal(alertNotice([alert({ last_error: null })]).detail, "acme-shop: no reason recorded", "never a blank reason");
console.log("  ok   the detail is the newest failure, and never blank");

console.log("alert notice rules passed");
