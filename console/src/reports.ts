// Wording and month arithmetic for the Reports tab. Pure, so it is tested (reports.test.mjs); the figures themselves
// come from the hub (hub/reports.py) and are never recomputed here.

import type { AvailabilityReport } from "./api/types.ts";

type Verdict = AvailabilityReport["clients"][number]["verdict"];

/** "2026-10" for a moment in time. Months are UTC, as the hub cuts them. */
export function monthOf(ms: number): string {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number);
  const index = y * 12 + (m - 1) + delta;
  return `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, "0")}`;
}

export function monthLabel(month: string): string {
  const [y, m] = month.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** The hub refuses a month that has not started, so the forward arrow stops at the current one. */
export const canGoForward = (month: string, nowMs: number) => month < monthOf(nowMs);

export function duration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${String(m).padStart(2, "0")} min` : `${h} h`;
}

/** No number is a dash, never 0% or 100%: a site nobody checked has no availability to claim. */
export const pctText = (p: number | null | undefined) => (p == null ? "—" : `${p.toFixed(2)}%`);

export const downtimeText = (minutes: number, checks: number) => (checks === 0 ? "—" : minutes === 0 ? "none" : `about ${duration(minutes)}`);

export const verdictTone = (v: Verdict): "ok" | "warn" | "bad" | "run" => ({ perfect: "ok", met: "ok", below: "bad", no_data: "run" } as const)[v];

export const verdictLabel = (v: Verdict) => ({ perfect: "No downtime", met: "Target met", below: "Below target", no_data: "No data" })[v];
