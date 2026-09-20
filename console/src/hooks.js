import { useCallback, useEffect, useState } from "react";

export const POLL_MS = 30_000;

// Fetch on mount and every `ms`. On failure the last good data is kept and
// `error` is set: a blank page during a hub restart would be worse than a
// slightly stale one, as long as the page says plainly that it is stale.
export function usePoll(url, ms = POLL_MS) {
  const [state, setState] = useState({ data: null, error: null, at: null });

  const load = useCallback(
    async (signal) => {
      try {
        const res = await fetch(url, { signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        setState({ data, error: null, at: Date.now() });
      } catch (err) {
        if (err.name === "AbortError") return;
        setState((prev) => ({ ...prev, error: err.message }));
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
export function useHashTab(valid, fallback) {
  const read = () => {
    const h = location.hash.replace(/^#\/?/, "");
    return valid.includes(h) ? h : fallback;
  };
  const [tab, setTab] = useState(read);
  useEffect(() => {
    const onHash = () => setTab(read());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  });
  const go = useCallback((next) => {
    location.hash = `/${next}`;
  }, []);
  return [tab, go];
}

// Writes. Throws with the hub's message so a form can show why it failed.
export async function send(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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
  return res.json();
}

export function ago(iso, now) {
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
export function settled(deploys) {
  const seen = new Set();
  return deploys.filter((d) => {
    const key = `${d.site}|${d.ref}`;
    const superseded = d.status === "running" && seen.has(key);
    seen.add(key);
    return !superseded;
  });
}

export const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
export const fmtTime = (iso) => new Date(iso).toLocaleString();

// 100 stays "100"; anything less is floored to one decimal, because toFixed
// would round 99.96 up to a healthy-looking "100.0".
export function fmtPct(p) {
  if (p == null) return "—";
  return p >= 100 ? "100" : (Math.floor(p * 10) / 10).toFixed(1);
}
