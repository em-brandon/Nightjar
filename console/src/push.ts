// The staging push, as its own card under the clock. It has four states and shows each for a while:
//
//   loading   files are going up (a running deploy, with a progress bar)
//   done      the deploy reported success: the files are delivered
//   complete  the staging stack came back healthy after that deploy: it is live and working
//   failed    the deploy failed
//
// "Done" and "complete" are different on purpose. A finished upload says nothing about whether the
// stack it fed is actually up, so "complete" needs the stack's own report as proof. Pure functions,
// so the timing rules can be tested without a browser. See push.test.mjs.

import type { Deploy, Stack } from "./api/types.ts";

export type PushState = "loading" | "done" | "complete" | "failed";
export type PushCard = {
  site: string;
  ref: string | null;
  ageMin: number;
  state: PushState;
  // 0..100, or null when the pipeline did not report one
  progress: number | null;
};

const MIN = 60_000;
// A "running" report older than an hour is a CI job that never said it finished (the same cut-off
// the dashboard uses for builds), so it is not shown as still going.
export const RUNNING_MAX_MIN = 60;
// How long each settled state stays on the card before it goes. A failure stays much longer,
// because a failure needs seeing.
export const DONE_SHOW_MIN = 10;
export const COMPLETE_SHOW_MIN = 5;
export const FAILED_SHOW_MIN = 60;

// Has the staging stack for this site reported healthy since the deploy? A silent reporter proves
// nothing, and a degraded stack is not complete.
export function stackHealthySince(stacks: Pick<Stack, "site" | "env" | "stale" | "state" | "reported_at">[], site: string, since: number): boolean {
  return stacks.some(
    (s) => s.site === site && s.env === "staging" && !s.stale && s.state === "running" && new Date(s.reported_at).getTime() >= since,
  );
}

/**
 * `deploys` is /api/deploys as the console holds it (already settled: a running row is dropped once
 * a newer row for the same site and ref exists), newest first; `stacks` is /api/stacks. Returns null
 * when there is nothing to show.
 */
export function pushView(deploys: Deploy[], now: number, stacks: Parameters<typeof stackHealthySince>[0] = []): PushCard | null {
  const d = deploys.find((x) => x.env === "staging");
  if (!d) return null;
  const at = new Date(d.created_at).getTime();
  const ageMin = (now - at) / MIN;
  const base = { site: d.site, ref: d.ref || null, ageMin };
  if (d.status === "running") return ageMin <= RUNNING_MAX_MIN ? { ...base, state: "loading", progress: d.progress ?? null } : null;
  if (d.status === "failed") return ageMin <= FAILED_SHOW_MIN ? { ...base, state: "failed", progress: null } : null;
  if (d.status === "success") {
    if (stackHealthySince(stacks, d.site, at)) return ageMin <= COMPLETE_SHOW_MIN ? { ...base, state: "complete", progress: 100 } : null;
    return ageMin <= DONE_SHOW_MIN ? { ...base, state: "done", progress: 100 } : null;
  }
  return null;
}

// ---------------------------------------------------------------- the sample

// A sample push, for judging the card while no real one is running. It walks through every state,
// each for its own time, then the card goes away and comes back after a pause. The pauses alternate
// 40 and 30 seconds. Change the numbers here to change the rhythm.
export const DEMO_LOADING_S = 12;
export const DEMO_DONE_S = 6;
export const DEMO_COMPLETE_S = 6;
export const DEMO_GAPS_S = [40, 30];

const SHOWN_S = DEMO_LOADING_S + DEMO_DONE_S + DEMO_COMPLETE_S;
export const DEMO_PERIOD_S = DEMO_GAPS_S.reduce((sum, gap) => sum + SHOWN_S + gap, 0);

export function demoPush(now: number): PushCard | null {
  let t = (now / 1000) % DEMO_PERIOD_S;
  for (const gap of DEMO_GAPS_S) {
    if (t < SHOWN_S) {
      const base = { site: "shop-staging", ref: "develop@a1b2c3d", ageMin: 0 };
      if (t < DEMO_LOADING_S) return { ...base, state: "loading", progress: Math.round((t / DEMO_LOADING_S) * 100) };
      if (t < DEMO_LOADING_S + DEMO_DONE_S) return { ...base, state: "done", progress: 100 };
      return { ...base, state: "complete", progress: 100 };
    }
    t -= SHOWN_S + gap;
    if (t < 0) return null; // in the pause
  }
  return null;
}
