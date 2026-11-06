// How the Reports tab words and steps through months. Run with: npm test

import assert from "node:assert/strict";
import { canGoForward, downtimeText, duration, monthLabel, monthOf, pctText, shiftMonth, verdictLabel, verdictTone } from "./reports.ts";

const nov15 = Date.UTC(2026, 10, 15, 12);
assert.equal(monthOf(nov15), "2026-11");
assert.equal(monthOf(Date.UTC(2026, 11, 31, 23, 59)), "2026-12", "months are UTC, like the hub's");
console.log("  ok   the current month is read in UTC");

assert.equal(shiftMonth("2026-11", -1), "2026-10");
assert.equal(shiftMonth("2026-01", -1), "2025-12", "back across a year");
assert.equal(shiftMonth("2025-12", 1), "2026-01", "forward across a year");
assert.equal(shiftMonth("2026-03", -14), "2025-01");
console.log("  ok   months step across year boundaries");

assert.equal(canGoForward("2026-10", nov15), true);
assert.equal(canGoForward("2026-11", nov15), false, "the hub refuses a month that has not started");
console.log("  ok   the forward arrow stops at the current month");

assert.equal(monthLabel("2026-10"), "October 2026");
assert.equal(monthLabel("2026-01"), "January 2026");
console.log("  ok   a month is named in words");

assert.equal(duration(0), "0 min");
assert.equal(duration(10), "10 min");
assert.equal(duration(60), "1 h");
assert.equal(duration(65), "1 h 05 min");
assert.equal(duration(190), "3 h 10 min");
console.log("  ok   a duration is minutes under an hour, hours and minutes above");

assert.equal(pctText(null), "—", "no checks is a dash, not a perfect score");
assert.equal(pctText(100), "100.00%");
assert.equal(pctText(99.9), "99.90%");
assert.equal(downtimeText(0, 0), "—", "nothing was checked, so downtime is unknown, not none");
assert.equal(downtimeText(0, 100), "none");
assert.equal(downtimeText(12, 100), "about 12 min");
console.log("  ok   a site nobody checked shows a dash, never 100% or 'no downtime'");

assert.deepEqual(["perfect", "met", "below", "no_data"].map(verdictTone), ["ok", "ok", "bad", "run"]);
assert.equal(verdictLabel("below"), "Below target");
console.log("  ok   each verdict has a tone and a label");
