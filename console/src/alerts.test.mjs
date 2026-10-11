// What the console says about alerts that did not reach the phone. Run with: npm test

import assert from "node:assert/strict";
import { alertNotice, alertState, senderHealth } from "./alerts.ts";

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

// a P1 that expired with nobody acknowledging it: sent, but the phone did not wake anyone
const unacked = (over) => alert({ status: "sent", last_error: null, expired_at: "2026-10-12T09:30:00Z", acked_at: null, title: "ProcessExit: died", ...over });
const u = alertNotice([unacked()]);
assert.equal(u.tone, "bad", "a P1 nobody answered is red");
assert.match(u.text, /^1 alert was not acknowledged, so your phone may not have woken you$/);
assert.equal(u.detail, "acme-shop: ProcessExit: died");
assert.match(alertNotice([unacked(), unacked({ id: 2 })]).text, /^2 alerts were not acknowledged/);
assert.equal(alertNotice([unacked({ acked_at: "2026-10-12T09:31:00Z" })]), null, "acknowledged after all: nothing to say");
assert.equal(alertNotice([unacked({ expired_at: null })]), null, "sent and still in flight is not a problem yet");
console.log("  ok   a P1 that expired unacknowledged is red and says the phone may not have woken anyone");

const both = alertNotice([alert(), unacked({ id: 2 }), alert({ id: 3, status: "pending" })]);
assert.equal(both.tone, "bad");
assert.equal(both.text, "1 alert could not be sent to your phone · 1 alert was not acknowledged, so your phone may not have woken you · 1 more being retried");
console.log("  ok   failed, unacknowledged and retrying are counted separately in one notice");

// ---- one alert's label
const st = (over) => alertState(alert(over));
assert.deepEqual(st({ status: "failed" }), { label: "failed", tone: "bad" });
assert.deepEqual(st({ status: "sent", priority: 0, acked_at: null, expired_at: null }), { label: "sent", tone: "ok" });
assert.equal(st({ status: "sent", priority: 2, acked_at: null, expired_at: null }).label, "sent · awaiting acknowledgement", "a P1 is not done until someone answers");
assert.deepEqual(st({ status: "sent", priority: 2, acked_at: "2026-10-12T09:01:00Z" }), { label: "acknowledged", tone: "ok" });
assert.deepEqual(st({ status: "sent", priority: 2, expired_at: "2026-10-12T09:30:00Z", acked_at: null }), { label: "not acknowledged", tone: "bad" });
assert.equal(st({ status: "pending", attempts: 0 }).label, "queued");
assert.deepEqual(st({ status: "pending", attempts: 2 }), { label: "retrying (attempt 2)", tone: "warn" });
assert.equal(st({ status: "sending", attempts: 1 }).label, "sending");
// an incident dealt with in the console: the repeating push is stopped, and an old expiry stops being news
assert.equal(st({ status: "sent", priority: 2, dismissed_at: "2026-10-12T09:05:00Z", cancelled_at: null, expired_at: null, acked_at: null }).label, "stopping the push");
assert.deepEqual(st({ status: "sent", priority: 2, dismissed_at: "2026-10-12T09:05:00Z", cancelled_at: "2026-10-12T09:05:04Z", acked_at: null }), { label: "stopped: incident dealt with", tone: "ok" });
assert.deepEqual(st({ status: "sent", priority: 2, expired_at: "2026-10-12T09:30:00Z", dismissed_at: "2026-10-12T10:00:00Z", acked_at: null, cancelled_at: null }), { label: "not acknowledged, dealt with later", tone: "warn" });
assert.equal(alertNotice([unacked({ dismissed_at: "2026-10-12T10:00:00Z" })]), null, "an expired P1 someone has since dealt with is not news");
console.log("  ok   each alert is labelled for what matters: failed, awaiting acknowledgement, not acknowledged, stopped");

// ---- the sender
const NOW = Date.parse("2026-10-12T09:00:00Z");
const facts = (over) => ({ sender_enabled: true, sender_poll_s: 5, sender_last_pass_at: "2026-10-12T08:59:56Z", sender_last_error: null, ...over });
assert.deepEqual(senderHealth(facts(), NOW), { state: "ok", tone: "ok", text: "running, last pass 4s ago" });
assert.equal(senderHealth(facts({ sender_enabled: false }), NOW).state, "off");
assert.equal(senderHealth(facts({ sender_enabled: false }), NOW).tone, "bad", "a switched-off sender is a problem, not a setting");
assert.equal(senderHealth(facts({ sender_last_pass_at: null }), NOW).state, "starting");
const stalled = senderHealth(facts({ sender_last_pass_at: "2026-10-12T08:55:00Z" }), NOW);
assert.deepEqual([stalled.state, stalled.tone, stalled.text], ["stalled", "bad", "stalled: last pass 5m ago"]);
assert.equal(senderHealth(facts({ sender_last_pass_at: "2026-10-12T08:59:40Z" }), NOW).state, "ok", "20s is within a few missed polls");
const failing = senderHealth(facts({ sender_last_error: "OperationalError" }), NOW);
assert.deepEqual([failing.state, failing.tone], ["ok", "warn"]);
assert.match(failing.text, /OperationalError/);
console.log("  ok   a sender that is off, stalled or failing is said plainly");

console.log("alert notice rules passed");
