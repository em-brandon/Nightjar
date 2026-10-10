// Which sites the Overview warns are not being checked. Run with: npm test

import assert from "node:assert/strict";
import { unwatched } from "./monitoring.ts";

const site = (name, env, monitored) => ({ name, env, monitored });

assert.deepEqual(unwatched([]), []);
assert.deepEqual(unwatched([site("a", "production", true)]), [], "a checked site is fine");
assert.deepEqual(unwatched([site("a", "production", false), site("b", "production", true)]), ["a"]);
console.log("  ok   production sites with no url are named");

assert.deepEqual(unwatched([site("a-staging", "staging", false)]), [], "staging has no url by design");
console.log("  ok   staging is not warned about");
