import { productionNameOf } from "./promotion.ts";

// Group a flat list of sites into one entry per client, and pair each production site with its staging site.
// A staging site belongs to a production site by NAME (`<production>-staging`, the same rule promotion uses),
// not by a column, which keeps sites independent. Pure, so the pairing is tested and every screen agrees.

export type Groupable = { name: string; env: string; client: string };

export type SiteGroup<T> = {
  // the production site's name, or the staging site's own name when it has no production site
  key: string;
  production: T | null;
  staging: T | null;
};

export type ClientGroup<T> = { client: string; groups: SiteGroup<T>[]; sites: T[] };

const byName = (a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name);

export function groupByClient<T extends Groupable>(sites: T[]): ClientGroup<T>[] {
  const clients = new Map<string, T[]>();
  for (const s of sites) clients.set(s.client, [...(clients.get(s.client) ?? []), s]);

  return [...clients.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([client, list]) => {
      const staging = list.filter((s) => s.env === "staging");
      const production = list.filter((s) => s.env !== "staging").sort(byName);
      const used = new Set<T>();

      const groups: SiteGroup<T>[] = production.map((p) => {
        const s = staging.find((x) => productionNameOf(x.name) === p.name) ?? null;
        if (s) used.add(s);
        return { key: p.name, production: p, staging: s };
      });
      // a staging site whose production site is missing, or belongs to someone else, is shown rather than lost
      for (const s of staging.filter((x) => !used.has(x)).sort(byName)) groups.push({ key: s.name, production: null, staging: s });

      return { client, groups, sites: list };
    });
}

// The worst state among a client's sites, for the dot on its card: down beats warn beats up.
export function worstState<T extends { state: "up" | "warn" | "down" }>(sites: T[]): "up" | "warn" | "down" {
  if (sites.some((s) => s.state === "down")) return "down";
  if (sites.some((s) => s.state === "warn")) return "warn";
  return "up";
}
