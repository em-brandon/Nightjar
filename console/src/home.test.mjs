// Home strip rules. Run with: npm test

import assert from "node:assert/strict";
import { ACTIVE_BPS, CYCLE_MS, TEMP_RANGES, activityView, backupView, demoHome, demoTraffic, fmtRate, pairAt, rangeText, tempState, tempsView, fmtAgeH, fmtAgeMin, fmtMbps, hostView, netView, presenceView, rateView, requestsView } from "./home.js";

const ok = (name) => console.log(`  ok   ${name}`);
const NOW = Date.parse("2026-09-21T12:00:00Z");
const ago = (min) => new Date(NOW - min * 60_000).toISOString();
const home = (over = {}) => ({
  stale: false, cpu_temp_c: 54, mem_total_mb: 24000, mem_available_mb: 12000, disk_total_mb: 1000, disk_free_mb: 600,
  backup_configured: true, backup_ok_at: ago(360), backup_age_h: 6,
  net: { tested_at: ago(20), down_mbps: 94.2, up_mbps: 21.6, latency_ms: 18.4 }, speed_series: [90, 94], ...over,
});

assert.equal(fmtMbps(94.24), "94.2");
assert.equal(fmtMbps(940.6), "941");
assert.equal(fmtMbps(null), "—");
assert.equal(fmtAgeH(0.4), "<1 h");
assert.equal(fmtAgeH(6.2), "6 h");
assert.equal(fmtAgeH(72), "3 days");
assert.equal(fmtAgeMin(0.2), "just now");
assert.equal(fmtAgeMin(45), "45 min");
assert.equal(fmtAgeMin(300), "5 h");
ok("formatting");

// a silent reporter is never current, whatever its last numbers said
assert.equal(netView(null, NOW), null);
assert.equal(netView(home({ stale: true }), NOW).state, "silent");
assert.equal(netView(home({ net: null }), NOW).state, "none");
assert.equal(netView(home(), NOW).state, "ok");
assert.equal(netView(home({ net: { tested_at: ago(200), down_mbps: 50 } }), NOW).state, "old");
const n = netView(home(), NOW);
assert.deepEqual([n.down, n.up, n.latency, n.age], ["94.2", "21.6", "18 ms", "20 min"]);
assert.equal(netView(home({ net: { tested_at: ago(5), down_mbps: 50, up_mbps: null, latency_ms: null } }), NOW).up, null);
ok("internet: silent, none, ok and old are told apart");

assert.equal(hostView(home({ stale: true })).state, "silent");
assert.equal(hostView(home()).state, "ok");
assert.equal(hostView(home({ cpu_temp_c: 82 })).state, "warm");
assert.equal(hostView(home({ cpu_temp_c: 88 })).state, "hot");
assert.equal(hostView(home({ disk_free_mb: 50 })).state, "low");
assert.equal(hostView(home({ cpu_temp_c: 90, disk_free_mb: 50 })).state, "hot", "the hotter problem wins");
assert.equal(hostView(home({ cpu_temp_c: null })).temp, null, "no sensor is not a temperature");
const hv = hostView(home());
assert.deepEqual([hv.temp, hv.diskFree, hv.memUsed], ["54°C", 60, 50]);
assert.equal(hostView(home({ disk_total_mb: null })).diskFree, null);
ok("host: hot, low disk and warm");

assert.equal(backupView(home({ stale: true })).state, "silent");
assert.equal(backupView(home({ backup_configured: false })).state, "untracked");
assert.equal(backupView(home({ backup_ok_at: null })).state, "missing", "tracked and nothing there is a problem");
assert.equal(backupView(home()).state, "ok");
assert.equal(backupView(home({ backup_age_h: 50 })).state, "stale");
assert.equal(backupView(home({ backup_age_h: 36 })).state, "ok", "exactly 36 h is not yet stale");
assert.equal(backupView(home()).text, "6 h ago");
ok("backup: not tracked is not the same as missing");

const t = (status, overdue = false) => ({ status, overdue });
assert.deepEqual(requestsView([]), { state: "clear", open: 0, overdue: 0, text: "inbox clear" });
assert.equal(requestsView([t("closed", true)]).state, "clear", "a closed request is never overdue");
assert.equal(requestsView([t("open"), t("open")]).text, "2 open");
const r = requestsView([t("open"), t("open", true), t("closed")]);
assert.deepEqual([r.state, r.open, r.overdue, r.text], ["overdue", 2, 1, "1 overdue"]);
ok("requests");

assert.equal(presenceView(null), null);
assert.equal(presenceView({ state: "away", source: null, recorded: false }).sub, "no tap recorded");
assert.equal(presenceView({ state: "away", source: "expired", recorded: true }).sub, "tap expired");
assert.equal(presenceView({ state: "away", source: "manual", recorded: true }).sub, null);
const p = presenceView({ state: "home", source: "nfc", expires_at: "2026-09-21T20:30:00Z", recorded: true });
assert.equal(p.state, "home");
assert.match(p.sub, /^until \d\d:\d\d$/);
ok("presence: away is the safe answer");

assert.equal(rateView(null), null);
assert.equal(rateView({ available: true, stale: false, base: "USD", quote: "KES", rate: 129.5712 }).value, "129.57");
assert.equal(rateView({ available: false }).value, null);
assert.equal(rateView({ available: false }).state, "off");
const good = { available: true, stale: false, base: "USD", quote: "KES", rate: 129.5712, pairs: [{ code: "USD", rate: 129.57 }, { code: "GBP", rate: 173.36 }, { code: "CNY", rate: 19.33 }] };
const rv = rateView(good);
assert.deepEqual(rv.pairs.map((p) => [p.code, p.symbol, p.value]), [["USD", "$", "129.57"], ["GBP", "£", "173.36"], ["CNY", "¥", "19.33"]]);
assert.equal(rateView({ available: true, stale: false, base: "USD", quote: "KES", rate: 129.5 }).pairs.length, 1, "an older feed with no pairs still shows the dollar");
assert.equal(rateView({ available: true, stale: true, base: "USD", quote: "KES", rate: 129 }).state, "old");
assert.equal(rateView({ available: true, stale: false, base: "USD", quote: "KES" }).state, "off", "available with no rate must not throw");
ok("exchange rate");

assert.equal(tempsView(null), null);
assert.deepEqual(tempsView(home({ stale: true })), { silent: true });
const tv = tempsView(home({ intake_c: 29.5, exhaust_c: 41.2 }));
assert.deepEqual([tv.cpu.value, tv.intake.value, tv.exhaust.value], [54, 30, 41]);
assert.deepEqual([tv.cpu.state, tv.intake.state, tv.exhaust.state], ["op", "op", "op"]);
const bare = tempsView(home());
assert.equal(bare.intake, null, "no cabinet sensor is no reading, not zero");
assert.equal(bare.exhaust, null);
assert.equal(tempsView(home({ cpu_temp_c: null })).cpu, null);
ok("temperatures: a missing sensor is absent, not zero");

// each device has its own range: cool below, operating within, high from the top figure
for (const kind of ["cpu", "intake", "exhaust"]) {
  const { cool, high } = TEMP_RANGES[kind];
  assert.equal(tempState(kind, cool - 0.1), "cool", `${kind} just below cool`);
  assert.equal(tempState(kind, cool), "op", `${kind} at the cool line is already operating`);
  assert.equal(tempState(kind, high - 0.1), "op", `${kind} just below high`);
  assert.equal(tempState(kind, high), "high", `${kind} at the high line`);
}
assert.equal(tempState("cpu", null), null);
assert.equal(tempState("nope", 30), null);
assert.equal(tempsView(home({ cpu_temp_c: 30 })).cpu.state, "cool");
assert.equal(tempsView(home({ cpu_temp_c: 82 })).cpu.state, "high");
assert.match(rangeText("cpu"), /Cool below 40°, operating 40–79°, high from 80°/);
assert.ok(TEMP_RANGES.intake.high < TEMP_RANGES.exhaust.high, "exhaust runs hotter than intake");
ok("temperature ranges: cool, operating and high per device");

// live network activity
const rate = (rx, tx, over = {}) => home({ rx_bps: rx, tx_bps: tx, ...over });
assert.equal(activityView(rate(0, 0), true).state, "offline", "the wall cannot reach the hub");
assert.equal(activityView(rate(9e6, 9e6, { stale: true })).state, "silent", "a silent reporter's last traffic is old news");
assert.equal(activityView(rate(1000, 1000)).state, "idle");
assert.equal(activityView(rate(ACTIVE_BPS, 0)).state, "down");
assert.equal(activityView(rate(0, ACTIVE_BPS)).state, "up");
assert.equal(activityView(rate(5e6, 2e6)).state, "both");
assert.equal(activityView(rate(ACTIVE_BPS - 1, ACTIVE_BPS - 1)).state, "idle", "background chatter is not activity");
assert.equal(activityView(home()).state, "idle", "no traffic figures: idle, and says so");
assert.equal(activityView(home()).text, "traffic not reported");
assert.equal(activityView(null), null);
assert.equal(activityView(null, true).state, "offline", "offline needs no home data at all");
assert.equal(fmtRate(3_200_000), "3.2 Mbps");
assert.equal(fmtRate(150_000), "150 kbps");
assert.equal(fmtRate(null), "—");
ok("network activity: offline, silent, idle, download, upload and both");

// the sample traffic walks through every state, so all the icons can be seen
const seen = new Set();
for (let t = 0; t < 16000; t += 4000) {
  const [rx, tx] = demoTraffic(t);
  seen.add(activityView(home({ rx_bps: rx, tx_bps: tx })).state);
}
assert.deepEqual([...seen].sort(), ["both", "down", "idle", "up"]);
ok("sample traffic cycles through every icon");

// the exchange rate takes turns
assert.deepEqual([0, 1, 2, 3, 4, 5].map((i) => pairAt(rv, i * CYCLE_MS).code), ["USD", "GBP", "CNY", "USD", "GBP", "CNY"]);
assert.equal(pairAt(rv, CYCLE_MS - 1).code, "USD", "still the first until its time is up");
assert.equal(pairAt(null, 0), null);
assert.equal(pairAt({ pairs: [] }, 0), null);
ok("the rate cycles through dollar, pound and yuan");

const demo = demoHome(NOW);
assert.ok(demo.every((d) => d.demo === true), "sample data must always be flagged");
assert.equal(netView(demo[0], NOW).state, "ok");
assert.equal(hostView(demo[0]).state, "ok");
assert.equal(backupView(demo[0]).state, "ok");
ok("sample data is flagged and reads healthy");

console.log("\nhome rules passed");
