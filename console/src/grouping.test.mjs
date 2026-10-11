// A client's sites, with each staging site paired to its production site. Run with: npm test

import assert from "node:assert/strict";
import { groupByClient, worstState } from "./grouping.ts";

const s = (name, env, client, state = "up") => ({ name, env, client, state });

const out = groupByClient([
  s("kubasu-motors-staging", "staging", "Kubasu Motors"),
  s("mwangaza-school", "production", "Mwangaza School"),
  s("kubasu-motors", "production", "Kubasu Motors"),
]);
assert.deepEqual(out.map((c) => c.client), ["Kubasu Motors", "Mwangaza School"], "clients are sorted by name");
const kubasu = out[0];
assert.equal(kubasu.groups.length, 1, "production and its staging are one group");
assert.deepEqual([kubasu.groups[0].production.name, kubasu.groups[0].staging.name], ["kubasu-motors", "kubasu-motors-staging"]);
assert.equal(out[1].groups[0].staging, null, "a production site with no staging says so with null, not a missing entry");
console.log("  ok   a staging site is paired with its production site under their client");

const multi = groupByClient([
  s("savanna-booking-staging", "staging", "Savanna"),
  s("savanna-safaris", "production", "Savanna"),
  s("savanna-booking", "production", "Savanna"),
])[0];
assert.deepEqual(multi.groups.map((g) => g.key), ["savanna-booking", "savanna-safaris"], "production sites are sorted");
assert.equal(multi.groups[0].staging.name, "savanna-booking-staging", "each staging goes with its own production site");
assert.equal(multi.groups[1].staging, null);
console.log("  ok   several production sites each get only their own staging");

const orphan = groupByClient([s("old-thing-staging", "staging", "Acme"), s("acme-shop", "production", "Acme")])[0];
assert.deepEqual(orphan.groups.map((g) => [g.key, g.production?.name ?? null, g.staging?.name ?? null]), [
  ["acme-shop", "acme-shop", null],
  ["old-thing-staging", null, "old-thing-staging"],
], "a staging site with no production site is shown, after the paired ones, and never lost");
console.log("  ok   an orphan staging site is kept");

// pairing is within a client: a staging site under another client is not stolen
const split = groupByClient([s("shop", "production", "A"), s("shop-staging", "staging", "B")]);
assert.equal(split[0].groups[0].staging, null);
assert.equal(split[1].groups[0].staging.name, "shop-staging");
console.log("  ok   staging is only paired within its own client");

assert.deepEqual(groupByClient([]), []);
assert.equal(kubasu.sites.length, 2, "the flat list is kept for counting");
console.log("  ok   empty in, empty out; the flat list is kept");

assert.equal(worstState([s("a", "production", "x", "up"), s("b", "staging", "x", "warn")]), "warn");
assert.equal(worstState([s("a", "production", "x", "warn"), s("b", "staging", "x", "down")]), "down");
assert.equal(worstState([]), "up");
console.log("  ok   a client's worst state is down, then warn, then up");

console.log("grouping rules passed");
