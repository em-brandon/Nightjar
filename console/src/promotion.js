// Is a staging environment fit to promote to production, and how does it differ
// from what production is running? Pure functions over the feeds the console
// already polls, so the rules can be tested without a browser.
//
// A staging site is paired with its production site by NAME: "<production>-staging"
// belongs to "<production>". A naming convention rather than a column in the
// database, which keeps sites independent, and is documented in the README.

const SUFFIX = /-staging$/;
export const productionNameOf = (name) => name.replace(SUFFIX, "");

const HOUR = 3_600_000;
// Below this a staging site is too flaky to trust a promotion decision to.
export const UPTIME_MIN = 99;

// "main@1f9c4a2" -> "1f9c4a2". Refs without an @ compare as a whole.
export const shaOf = (ref) => (ref || "").split("@").pop();

// Floor, not round: 98.96% must not read as 99.
const pct = (p) => (Math.floor(p * 10) / 10).toFixed(1);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// Builds still in flight. A "running" report older than an hour is a CI job that
// never said it finished, so it does not count as a build using the machine.
export function runningBuilds(deploys, now) {
  return deploys.filter((d) => d.status === "running" && now - new Date(d.created_at) < HOUR);
}

/**
 * @param site    a staging entry from /api/sites/health
 * @param sites   every site from /api/sites/health
 * @param deploys /api/deploys, newest first
 * @param stacks  /api/stacks
 * @returns {{verdict: "ready"|"blocked"|"insync"|"behind", blockers: string[], compare: object}}
 */
export function readiness({ site, sites, deploys, stacks, now }) {
  const blockers = [];

  const mine = deploys.filter((d) => d.site === site.name);
  const last = mine[0];
  if (!last) blockers.push("no deploy recorded");
  else if (last.status === "failed") blockers.push("last deploy failed");
  else if (last.status === "running") {
    blockers.push(now - new Date(last.created_at) > HOUR ? "deploy never reported finishing" : "deploy in progress");
  }

  if (!site.monitored) blockers.push("no health check");
  else if (site.last_check == null || site.probe_stale) blockers.push("health check silent");
  else if (site.uptime_24h != null && site.uptime_24h < UPTIME_MIN) {
    blockers.push(`uptime ${pct(site.uptime_24h)}% over 24h`);
  }

  const open = site.open_p1 + site.open_p2;
  if (open > 0) blockers.push(`${plural(open, "open incident", "open incidents")}`);

  // Stacks are optional: a site whose reporter is not set up is not blocked for it.
  // But a reporter that has gone quiet is not evidence of health.
  const own = stacks.filter((s) => s.site === site.name);
  if (own.some((s) => s.stale)) blockers.push("stack reporter silent");
  if (own.some((s) => !s.stale && s.state === "degraded")) blockers.push("stack degraded");
  if (own.some((s) => !s.stale && s.restart_loop)) blockers.push("stack restarting repeatedly");

  return { blockers, compare: compare(site, sites, mine, deploys), ...verdictOf(blockers, site, sites, deploys) };
}

function lastSuccess(deploys, name) {
  return deploys.find((d) => d.site === name && d.status === "success");
}

function compare(site, sites, mine, deploys) {
  const prodName = productionNameOf(site.name);
  const prodSite = sites.find((s) => s.env === "production" && s.name === prodName);
  const staging = lastSuccess(mine, site.name);
  const production = prodSite ? lastSuccess(deploys, prodSite.name) : undefined;

  if (!prodSite) return { state: "unpaired", text: `no production site named ${prodName}`, staging, production };
  if (!production) return { state: "unknown", text: "production has no successful deploy recorded", staging, production };
  if (!staging) return { state: "unknown", text: "staging has no successful deploy recorded", staging, production };

  const same = shaOf(staging.ref) && shaOf(staging.ref) === shaOf(production.ref);
  if (same) return { state: "insync", text: "staging and production run the same commit", staging, production };
  if (new Date(production.created_at) > new Date(staging.created_at)) {
    return { state: "behind", text: "production was deployed after staging", staging, production };
  }
  return { state: "ahead", text: "staging is ahead of production", staging, production };
}

function verdictOf(blockers, site, sites, deploys) {
  const c = compare(site, sites, deploys.filter((d) => d.site === site.name), deploys);
  // Blockers win. Only a clean staging can be "nothing to promote" or "behind".
  if (blockers.length) return { verdict: "blocked" };
  if (c.state === "insync") return { verdict: "insync" };
  if (c.state === "behind") return { verdict: "behind" };
  return { verdict: "ready" };
}
