import { useCallback, useEffect, useRef, useState } from "react";
import type { Deploy } from "./api/types.ts";

export const POLL_MS = 30_000;

// A 401 means the session ended (or was never there). Root listens for this and
// shows the login, instead of every feed reporting "can't reach the hub".
export const announceUnauthorized = () => window.dispatchEvent(new Event("nj-unauthorized"));

// Idle-screen timings. `?idle=5&rotate=8&reveal=10` (seconds) shortens them for
// testing, but only in the dev server: a production build ignores the URL, so a
// query string can never lengthen how long details stay revealed.
export function tuning(key: string, fallbackMs: number): number {
  if (!import.meta.env.DEV) return fallbackMs;
  const v = Number(new URLSearchParams(location.search).get(key));
  return v > 0 ? v * 1000 : fallbackMs;
}

// Fetch on mount and every `ms`. On failure the last good data is kept and
// `error` is set: a blank page during a hub restart would be worse than a
// slightly stale one, as long as the page says plainly that it is stale.
export type Poll<T> = { data: T | null; error: string | null; at: number | null; reload: () => Promise<void> };

export function usePoll<T = unknown>(url: string, ms = POLL_MS): Poll<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; at: number | null }>({ data: null, error: null, at: null });

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const res = await fetch(url, { signal });
        if (res.status === 401) announceUnauthorized();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as T;
        setState({ data, error: null, at: Date.now() });
      } catch (err) {
        // fetch and res.json reject with an Error (a DOMException for an abort), so name and message exist
        const e = err as Error;
        if (e.name === "AbortError") return;
        setState((prev) => ({ ...prev, error: e.message }));
      }
    },
    [url],
  );

  useEffect(() => {
    const ctl = new AbortController();
    load(ctl.signal);
    const id = setInterval(() => load(ctl.signal), ms);
    return () => {
      ctl.abort();
      clearInterval(id);
    };
  }, [load, ms]);

  const reload = useCallback(() => load(), [load]);
  return { ...state, reload };
}

// Re-render every few seconds so "12s ago" labels do not freeze between polls.
export function useNow(ms = 5000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

// Tabs live in the URL hash so a refresh keeps you where you were, without a
// router dependency for five pages.
export function useHashTab<T extends string>(valid: readonly T[], fallback: T): [T, (next: T) => void] {
  const read = () => {
    const h = location.hash.replace(/^#\/?/, "");
    return (valid as readonly string[]).includes(h) ? (h as T) : fallback;
  };
  const [tab, setTab] = useState(read);
  useEffect(() => {
    const onHash = () => setTab(read());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  });
  const go = useCallback((next: T) => {
    location.hash = `/${next}`;
  }, []);
  return [tab, go];
}

// Writes. Throws with the hub's message so a form can show why it failed.
export async function send<T = unknown>(method: string, url: string, body?: unknown): Promise<T | null> {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) announceUnauthorized();
  if (!res.ok) {
    let why = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (typeof j.detail === "string") why = j.detail;
    } catch {
      /* keep the status text */
    }
    throw new Error(why);
  }
  return res.status === 204 ? null : ((await res.json()) as T);
}

export function ago(iso: string | null | undefined, now: number): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// CI reports each status as its own row, so a deploy that went running ->
// success would otherwise leave a stale "running" behind it. Newest-first in,
// and a running row is dropped once a newer row exists for the same site+ref.
// Failed rows are kept: a retry must not erase the failure it retried.
export function settled(deploys: Deploy[]): Deploy[] {
  const seen = new Set<string>();
  return deploys.filter((d) => {
    const key = `${d.site}|${d.ref}`;
    const superseded = d.status === "running" && seen.has(key);
    seen.add(key);
    return !superseded;
  });
}

export const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
export const fmtTime = (iso: string) => new Date(iso).toLocaleString();

// 100 stays "100"; anything less is floored to one decimal, because toFixed
// would round 99.96 up to a healthy-looking "100.0".
export function fmtPct(p: number | null | undefined): string {
  if (p == null) return "—";
  return p >= 100 ? "100" : (Math.floor(p * 10) / 10).toFixed(1);
}

// Calls `onIdle` once after `ms` with no pointer, key, wheel or touch input.
// Input events are throttled to one reset per second: pointermove fires
// hundreds of times a second and resetting a timer on each would be wasteful.
export function useInactivity(ms: number, onIdle: () => void, enabled = true): void {
  const cb = useRef(onIdle);
  cb.current = onIdle;
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout> = setTimeout(() => cb.current(), ms);
    let last = Date.now();
    const reset = () => {
      if (Date.now() - last < 1000) return;
      last = Date.now();
      clearTimeout(timer);
      timer = setTimeout(() => cb.current(), ms);
    };
    const events = ["pointermove", "pointerdown", "keydown", "wheel", "touchstart"];
    events.forEach((e) => addEventListener(e, reset, { passive: true }));
    return () => {
      clearTimeout(timer);
      events.forEach((e) => removeEventListener(e, reset));
    };
  }, [ms, enabled]);
}

// Whether the idle screen is showing detail. Detail reverts on its own after
// `ms`, never on a button: you will walk away without pressing anything.
//
// Today `reveal()` is called by a labelled "Simulate fingerprint" control. When
// the R307 reader exists (chunk 6) the hub will report a match and this is the
// one place to swap: the rest of the screen only reads `revealed`.
export function useReveal(ms: number, now: number) {
  const [until, setUntil] = useState(0);
  const remaining = Math.max(0, until - now);
  return {
    revealed: remaining > 0,
    remainingMs: remaining,
    reveal: () => setUntil(Date.now() + ms),
    hide: () => setUntil(0),
  };
}
