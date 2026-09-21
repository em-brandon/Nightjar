// Rules for promotion readiness. Run with: npm test
import assert from "node:assert/strict";
import { productionNameOf, readiness, runningBuilds, shaOf } from "./promotion.js";

const NOW = Date.parse("2026-09-21T12:00:00Z");
const ago = (min) => new Date(NOW - min * 60_000).toISOString();

const site = (o = {}) => ({
  name: "shop-staging", env: "staging", monitored: true, probe_stale: false, last_check: ago(1),
  uptime_24h: 100, open_p1: 0, open_p2: 0, ...o,
});
const prod = { name: "shop", env: "production" };
const dep = (o) => ({ id: Math.random(), site: "shop-staging", ref: "develop@bbb", status: "success", created_at: ago(60), ...o });
const run = (o = {}) => readiness({ site: site(o.site), sites: o.sites ?? [site(o.site), prod], deploys: o.deploys ?? [], stacks: o.stacks ?? [], now: NOW });

let n = 0;
const t = (name, fn) => { fn(); n++; console.log("  ok  ", name); };

t("pairing convention and sha parsing", () => {
  assert.equal(productionNameOf("shop-staging"), "shop");
  assert.equal(productionNameOf("shop"), "shop");
  assert.equal(shaOf("main@1f9c4a2"), "1f9c4a2");
  assert.equal(shaOf(""), "");
});

t("ready: clean staging, newer than production", () => {
  const r = run({ deploys: [dep({ ref: "develop@new", created_at: ago(30) }), dep({ site: "shop", ref: "main@old", created_at: ago(300) })] });
  assert.deepEqual(r.blockers, []);
  assert.equal(r.verdict, "ready");
  assert.equal(r.compare.state, "ahead");
});

t("in sync: same commit on both is 'nothing to promote', not 'ready'", () => {
  const r = run({ deploys: [dep({ ref: "develop@same" }), dep({ site: "shop", ref: "main@same", created_at: ago(500) })] });
  assert.equal(r.verdict, "insync");
});

t("behind: production deployed after staging", () => {
  const r = run({ deploys: [dep({ ref: "develop@a", created_at: ago(600) }), dep({ site: "shop", ref: "main@b", created_at: ago(5) })] });
  assert.equal(r.verdict, "behind");
});

t("blocked: failed deploy", () => {
  const r = run({ deploys: [dep({ status: "failed" })] });
  assert.equal(r.verdict, "blocked");
  assert.ok(r.blockers.includes("last deploy failed"));
});

t("a failed deploy blocks even if an older one succeeded", () => {
  const r = run({ deploys: [dep({ status: "failed", created_at: ago(5) }), dep({ status: "success", created_at: ago(90) })] });
  assert.ok(r.blockers.includes("last deploy failed"));
});

t("blocked: deploy in progress, and one that never finished", () => {
  assert.ok(run({ deploys: [dep({ status: "running", created_at: ago(3) })] }).blockers.includes("deploy in progress"));
  assert.ok(run({ deploys: [dep({ status: "running", created_at: ago(300) })] }).blockers.includes("deploy never reported finishing"));
});

t("blocked: no deploys at all", () => {
  assert.ok(run().blockers.includes("no deploy recorded"));
});

t("health: not monitored, silent, and low uptime (98.96 must not read as 99)", () => {
  const d = [dep()];
  assert.ok(run({ site: { monitored: false }, deploys: d }).blockers.includes("no health check"));
  assert.ok(run({ site: { probe_stale: true }, deploys: d }).blockers.includes("health check silent"));
  assert.ok(run({ site: { last_check: null }, deploys: d }).blockers.includes("health check silent"));
  const low = run({ site: { uptime_24h: 98.96 }, deploys: d });
  assert.ok(low.blockers.some((b) => b === "uptime 98.9% over 24h"), low.blockers.join());
  assert.equal(run({ site: { uptime_24h: 99 }, deploys: d }).blockers.length, 0);
});

t("open incidents block, with correct plural", () => {
  const d = [dep()];
  assert.ok(run({ site: { open_p1: 1 }, deploys: d }).blockers.includes("1 open incident"));
  assert.ok(run({ site: { open_p2: 2 }, deploys: d }).blockers.includes("2 open incidents"));
});

t("stacks: silent, degraded and restart loop block; no stack at all does not", () => {
  const d = [dep()];
  const mk = (o) => [{ site: "shop-staging", stale: false, state: "running", restart_loop: false, ...o }];
  assert.ok(run({ deploys: d, stacks: mk({ stale: true }) }).blockers.includes("stack reporter silent"));
  assert.ok(run({ deploys: d, stacks: mk({ state: "degraded" }) }).blockers.includes("stack degraded"));
  assert.ok(run({ deploys: d, stacks: mk({ restart_loop: true }) }).blockers.includes("stack restarting repeatedly"));
  assert.equal(run({ deploys: d, stacks: [] }).blockers.length, 0);
  // a stack belonging to a DIFFERENT site must not block this one
  assert.equal(run({ deploys: d, stacks: [{ site: "other", stale: true, state: "degraded" }] }).blockers.length, 0);
});

t("blockers beat 'in sync': a broken staging is blocked, not 'nothing to promote'", () => {
  const r = run({ deploys: [dep({ ref: "develop@same" }), dep({ site: "shop", ref: "main@same", created_at: ago(500) })], site: { open_p1: 1 } });
  assert.equal(r.verdict, "blocked");
});

t("unpaired / unknown comparisons never claim 'ahead'", () => {
  const noProd = run({ sites: [site()], deploys: [dep()] });
  assert.equal(noProd.compare.state, "unpaired");
  assert.equal(noProd.verdict, "ready");        // can't compare, but nothing blocks it
  const noProdDeploy = run({ deploys: [dep()] });
  assert.equal(noProdDeploy.compare.state, "unknown");
});

t("runningBuilds: counts live builds, ignores abandoned ones", () => {
  const ds = [
    { status: "running", created_at: ago(5) }, { status: "running", created_at: ago(20) },
    { status: "running", created_at: ago(400) }, { status: "success", created_at: ago(1) },
  ];
  assert.equal(runningBuilds(ds, NOW).length, 2);
});

console.log(`\n${n} groups passed`);
