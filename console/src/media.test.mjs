// Home media rules. Run with: npm test

import assert from "node:assert/strict";
import { byUser, demoMedia, diskFreePct, fmtDuration, label, mediaState, progress, topTitles, within } from "./media.js";

const ok = (name) => console.log(`  ok   ${name}`);
const server = (over = {}) => ({ stale: false, up: true, host_disk_total_mb: 1000, host_disk_free_mb: 600, ...over });

// silence is not health, and is not "idle"
assert.equal(mediaState(server({ stale: true })), "silent");
assert.equal(mediaState(server({ stale: true, up: false })), "silent", "a silent reporter's old 'down' is not current");
assert.equal(mediaState(server({ up: false })), "down");
assert.equal(mediaState(server({ host_disk_free_mb: 50 })), "low");
assert.equal(mediaState(server({ host_disk_free_mb: 100 })), "up", "exactly 10% free is not low");
assert.equal(mediaState(server({ host_disk_total_mb: null })), "up", "no disk reported is not a low disk");
ok("a silent reporter is never 'up', and down and low disk are told apart");

assert.equal(diskFreePct({ host_disk_total_mb: 0, host_disk_free_mb: 0 }), null);
assert.equal(diskFreePct(server()), 60);
ok("disk percentage guards against a missing total");

assert.equal(label({ series: "Friends", episode: "S01E04", title: "The Pilot" }), "Friends S01E04: The Pilot");
assert.equal(label({ series: "Friends", episode: null, title: "The Pilot" }), "Friends: The Pilot");
assert.equal(label({ series: null, title: "A Film" }), "A Film");
ok("labels for episodes, untagged episodes and films");

assert.equal(fmtDuration(null), "—");
assert.equal(fmtDuration(20), "<1m");
assert.equal(fmtDuration(45 * 60), "45m");
assert.equal(fmtDuration(72 * 60), "1h 12m");
assert.equal(fmtDuration(120 * 60), "2h");
ok("durations");

assert.equal(progress({ position_s: 660, runtime_s: 1320 }), 50);
assert.equal(progress({ position_s: 5000, runtime_s: 1320 }), 100, "never past the end");
assert.equal(progress({ position_s: 10, runtime_s: null }), null);
ok("progress");

const NOW = Date.parse("2026-09-21T12:00:00Z");
const day = (n) => new Date(NOW - n * 86_400_000).toISOString();
const plays = [
  { user: "amani", series: "Friends", title: "a", started_at: day(1), watched_s: 1200 },
  { user: "amani", series: "Friends", title: "b", started_at: day(2), watched_s: 1300 },
  { user: "baraka", series: "The Office", title: "c", started_at: day(3), watched_s: 600 },
  { user: "baraka", series: null, title: "A Film", started_at: day(20), watched_s: 5400 },
];
assert.equal(within(plays, 7, NOW).length, 3);
assert.equal(within(plays, 30, NOW).length, 4);
ok("history windows");

const users = byUser(plays);
assert.deepEqual(users.map((u) => [u.user, u.plays, u.watched_s]), [["baraka", 2, 6000], ["amani", 2, 2500]]);
assert.deepEqual(byUser([]), []);
ok("per-person totals, most watched first");

const titles = topTitles(plays);
assert.deepEqual(titles.map((t) => [t.name, t.plays]), [["A Film", 1], ["Friends", 2], ["The Office", 1]]);
assert.equal(topTitles(plays, 1).length, 1);
ok("a series counts once however many episodes");

const demo = demoMedia(NOW);
assert.ok(demo.every((m) => m.demo === true), "sample data must always be flagged");
assert.equal(mediaState(demo[0]), "up");
assert.ok(demo[0].sessions.length > 0);
ok("sample data is flagged as sample");

console.log("\nmedia rules passed");
