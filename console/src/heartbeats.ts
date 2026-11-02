import type { Heartbeat } from "./api/types.ts";

// How the Heartbeats tab reads the hub's feed. The hub decides the state (hub/heartbeats.py `state_of`);
// this only orders and words it, so the two cannot disagree about what is wrong.

const RANK: Record<Heartbeat["state"], number> = { failed: 0, missed: 1, late: 2, ok: 3 };

/** "every 5 min", "every 1 d": the schedule a heartbeat promised. */
export function every(interval_s: number): string {
  if (interval_s % 86400 === 0) return `every ${interval_s / 86400} d`;
  if (interval_s % 3600 === 0) return `every ${interval_s / 3600} h`;
  if (interval_s % 60 === 0) return `every ${interval_s / 60} min`;
  return `every ${interval_s} s`;
}

/** A failed or missed heartbeat has an open issue behind it. `late` is not one yet: nothing has alerted. */
export const isProblem = (h: Pick<Heartbeat, "state">): boolean => h.state === "failed" || h.state === "missed";

/** Problems first, then by how worried to be, then by name, so a silent job is never below the fold. */
export function ordered(list: Heartbeat[]): Heartbeat[] {
  return [...list].sort((a, b) => RANK[a.state] - RANK[b.state] || a.site.localeCompare(b.site) || a.name.localeCompare(b.name));
}

/** The sidebar badge. Hot (red) only when a production app, which stands in for uptime, is in trouble. */
export function badge(list: Heartbeat[]): { n: number; hot: boolean } {
  const bad = list.filter(isProblem);
  return { n: bad.length, hot: bad.some((h) => h.kind === "app" && h.env === "production") };
}

/** When the next ping is expected, as epoch ms. */
export const dueAt = (h: Pick<Heartbeat, "last_ping_at" | "interval_s">): number => new Date(h.last_ping_at).getTime() + h.interval_s * 1000;
