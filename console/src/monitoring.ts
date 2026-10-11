import type { SiteHealth } from "./api/types.ts";

// Production sites nobody is checking. A site with no url still reports crashes, but it has no
// uptime ring, so "All quiet" would be a claim nothing is backing. Staging is left out on purpose:
// the hub on the VPS cannot reach the home server, so a staging site is watched by its stack
// reporter instead and having no url is normal there.
export function unwatched(sites: Pick<SiteHealth, "name" | "env" | "monitored">[]): string[] {
  return sites.filter((s) => s.env === "production" && !s.monitored).map((s) => s.name);
}
