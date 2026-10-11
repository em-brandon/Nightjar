// Home host, internet, backup, requests, presence and rate helpers for the idle screen's strip and
// the dashboard. Pure functions, so the judgement calls can be tested without a browser.
// See home.test.mjs.

import type { HomeSnapshot, Presence, Rates, Ticket } from "./api/types.ts";

type Home = HomeSnapshot | null | undefined;

// Sample data is a snapshot that says so, so nothing that draws it can count it as real.
export type DemoHome = HomeSnapshot & { demo: true };

export const CPU_HOT_C = 85;
export const CPU_WARM_C = 80;
export const DISK_LOW_PCT = 10;
export const BACKUP_STALE_H = 36;
// The reporter tests the line hourly. Three hours with no new test means the test has stopped.
export const SPEED_OLD_MIN = 180;

export const fmtMbps = (m: number | null | undefined) => (m == null ? "—" : m >= 100 ? String(Math.round(m)) : m.toFixed(1));

export function fmtAgeH(hours: number | null | undefined): string {
  if (hours == null) return "—";
  if (hours < 1) return "<1 h";
  if (hours < 48) return `${Math.round(hours)} h`;
  return `${Math.round(hours / 24)} days`;
}

export function fmtAgeMin(minutes: number | null | undefined): string {
  if (minutes == null) return "—";
  if (minutes < 1) return "just now";
  if (minutes < 120) return `${Math.round(minutes)} min`;
  return `${Math.round(minutes / 60)} h`;
}

const pct = (part: number | null, total: number | null) => (total ? ((part ?? 0) / total) * 100 : null);

// ---------------------------------------------------------------- internet

// A silent reporter shows nothing current: its last test is old news, and dressing it as the
// present would say the internet is fine while the machine that measures it is not answering.
export function netView(h: Home, now: number) {
  if (!h) return null;
  if (h.stale) return { state: "silent" as const, text: "reporter silent" };
  if (!h.net) return { state: "none" as const, text: "no test yet" };
  const ageMin = (now - new Date(h.net.tested_at).getTime()) / 60_000;
  return {
    state: ageMin > SPEED_OLD_MIN ? ("old" as const) : ("ok" as const),
    down: fmtMbps(h.net.down_mbps),
    up: h.net.up_mbps == null ? null : fmtMbps(h.net.up_mbps),
    latency: h.net.latency_ms == null ? null : `${Math.round(h.net.latency_ms)} ms`,
    age: fmtAgeMin(ageMin),
    series: h.speed_series,
  };
}

// ---------------------------------------------------------------- the host

export function hostView(h: Home) {
  if (!h) return null;
  if (h.stale) return { state: "silent", text: "reporter silent" };
  const diskFree = pct(h.disk_free_mb, h.disk_total_mb);
  // pct is only null for a zero or missing total, which the old truthiness check excluded
  const memFree = pct(h.mem_available_mb, h.mem_total_mb);
  const memUsed = memFree == null ? null : 100 - memFree;
  let state: "ok" | "hot" | "low" | "warm" = "ok";
  if (h.cpu_temp_c != null && h.cpu_temp_c >= CPU_HOT_C) state = "hot";
  else if (diskFree != null && diskFree < DISK_LOW_PCT) state = "low";
  else if (h.cpu_temp_c != null && h.cpu_temp_c >= CPU_WARM_C) state = "warm";
  return {
    state,
    temp: h.cpu_temp_c == null ? null : `${Math.round(h.cpu_temp_c)}°C`,
    diskFree: diskFree == null ? null : Math.round(diskFree),
    memUsed: memUsed == null ? null : Math.round(memUsed),
  };
}

// ---------------------------------------------------------------- temperatures

// Each device has its own range, and three colours: COOL (blue) for cold, OPERATING (amber) for its
// normal working temperature, HIGH (red) for too hot. A reading below `cool` is cool, from `cool` up
// to but not including `high` is operating, and `high` or above is high. Change a number here and
// everything follows. These are working figures, not measurements: the CPU's `high` is the cabinet
// design's own sustained target (under 80 C), and the two air ranges are reasoned for a small PC in
// a warm room and should be adjusted once the cabinet has been measured under load.
export type TempKind = "cpu" | "intake" | "exhaust";

export const TEMP_RANGES: Record<TempKind, { label: string; cool: number; high: number }> = {
  cpu: { label: "ThinkCentre CPU", cool: 40, high: 80 },
  intake: { label: "Cabinet intake", cool: 20, high: 35 },
  exhaust: { label: "Cabinet exhaust", cool: 28, high: 45 },
};

export function tempState(kind: TempKind, celsius: number | null | undefined): "cool" | "op" | "high" | null {
  const r = TEMP_RANGES[kind];
  if (celsius == null || !r) return null;
  return celsius < r.cool ? "cool" : celsius >= r.high ? "high" : "op";
}

export const rangeText = (kind: TempKind) => {
  const r = TEMP_RANGES[kind];
  return `Cool below ${r.cool}°, operating ${r.cool}–${r.high - 1}°, high from ${r.high}°`;
};

// A machine with no cabinet sensors simply has no intake or exhaust.
export function tempsView(h: Home) {
  if (!h) return null;
  if (h.stale) return { silent: true };
  const one = (kind: TempKind, c: number | null) => (c == null ? null : { value: Math.round(c), state: tempState(kind, c), range: rangeText(kind) });
  return { cpu: one("cpu", h.cpu_temp_c), intake: one("intake", h.intake_c), exhaust: one("exhaust", h.exhaust_c) };
}

// ---------------------------------------------------------------- live network activity

// Traffic above this, in either direction, counts as the line being in use. Below it is background chatter.
export const ACTIVE_BPS = 200_000;

export function fmtRate(bps: number | null | undefined): string {
  if (bps == null) return "—";
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mbps`;
  return `${Math.round(bps / 1e3)} kbps`;
}

// What the internet icon shows, worst news first:
//   offline: the wall itself cannot reach the hub, so the home connection is probably down;
//   silent: the hub is fine but the home reporter has stopped, so nothing is known about the line;
//   down, up, both, idle: the last minute's traffic. The reporter averages over a minute, so this
//   is "busy lately", not a live meter.
export function activityView(h: Home, offline = false) {
  if (offline) return { state: "offline", text: "No connection: the wall cannot reach the hub" };
  if (!h) return null;
  if (h.stale) return { state: "silent", text: "reporter silent" };
  const rx = h.rx_bps ?? null;
  const tx = h.tx_bps ?? null;
  const down = rx != null && rx >= ACTIVE_BPS;
  const up = tx != null && tx >= ACTIVE_BPS;
  const state = down && up ? "both" : down ? "down" : up ? "up" : "idle";
  const text = rx == null && tx == null ? "traffic not reported" : `↓ ${fmtRate(rx)}  ↑ ${fmtRate(tx)}`;
  return { state, rx, tx, text };
}

// Sample traffic cycles through every state, so the icons can be seen changing while the home
// reporter is not connected.
const DEMO_TRAFFIC: [number, number][] = [
  [40_000, 15_000],
  [3_200_000, 90_000],
  [80_000, 1_500_000],
  [4_100_000, 1_200_000],
];
export const demoTraffic = (now: number): [number, number] => DEMO_TRAFFIC[Math.floor(now / 4000) % DEMO_TRAFFIC.length];

// ---------------------------------------------------------------- backup

export function backupView(h: Home) {
  if (!h) return null;
  if (h.stale) return { state: "silent" as const, text: "reporter silent" };
  // "not tracked" and "tracked, and nothing there" are different: the second is a problem.
  if (!h.backup_configured) return { state: "untracked" as const, text: "not tracked" };
  if (h.backup_ok_at == null) return { state: "missing" as const, text: "no backup found" };
  const age = fmtAgeH(h.backup_age_h);
  return (h.backup_age_h ?? 0) > BACKUP_STALE_H
    ? { state: "stale" as const, text: `${age} old`, age }
    : { state: "ok" as const, text: `${age} ago`, age };
}

// ---------------------------------------------------------------- client requests

export function requestsView(tickets: Pick<Ticket, "status" | "overdue">[]) {
  const open = tickets.filter((t) => t.status === "open");
  const overdue = open.filter((t) => t.overdue).length;
  if (open.length === 0) return { state: "clear", open: 0, overdue: 0, text: "inbox clear" };
  return {
    state: overdue > 0 ? "overdue" : "open",
    open: open.length,
    overdue,
    text: overdue > 0 ? `${overdue} overdue` : `${open.length} open`,
  };
}

// ---------------------------------------------------------------- presence

const clock = (t: string) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

// Away is the safe answer: with no tap, or an expired one, nobody has said they are home.
export function presenceView(p: Presence | null | undefined) {
  if (!p) return null;
  if (p.state === "home") return { state: "home", text: "Home", sub: p.expires_at ? `until ${clock(p.expires_at)}` : null };
  if (p.source === "expired") return { state: "away", text: "Away", sub: "tap expired" };
  return { state: "away", text: "Away", sub: p.recorded ? null : "no tap recorded" };
}

// ---------------------------------------------------------------- exchange rate

const SYMBOL: Record<string, string> = { USD: "$", GBP: "£", CNY: "¥" };
const NAME: Record<string, string> = { USD: "US dollar", GBP: "pound", CNY: "yuan" };
export const CYCLE_MS = 5000;

export function rateView(r: Rates | null | undefined) {
  if (!r) return null;
  // available but with no rate is as good as unavailable: reading it would throw and blank the wall
  if (!r.available || r.rate == null) return { state: "off", value: null, pairs: [], text: "rate unavailable" };
  const pairs = (r.pairs?.length ? r.pairs : [{ code: r.base, rate: r.rate }]).map((p) => ({
    code: p.code,
    symbol: SYMBOL[p.code] ?? p.code,
    name: NAME[p.code] ?? p.code,
    value: p.rate.toFixed(2),
  }));
  return { state: r.stale ? "old" : "ok", value: r.rate.toFixed(2), pairs, text: `${r.quote} ${r.rate.toFixed(2)}`, sub: `per ${r.base}` };
}

// Which currency is on show now: each gets CYCLE_MS in turn.
export function pairAt<P>(view: { pairs?: P[] } | null | undefined, now: number): P | null {
  if (!view?.pairs?.length) return null;
  return view.pairs[Math.floor(now / CYCLE_MS) % view.pairs.length];
}

// ---------------------------------------------------------------- sample data

// Shown, tagged `demo`, until a real home reporter is connected. Never counted in anything.
export function demoHome(now: number = Date.now()): DemoHome[] {
  const at = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
  return [
    {
      site: "thinkcentre",
      demo: true,
      stale: false,
      reported_at: at(0),
      cpu_temp_c: 54,
      intake_c: 29.5,
      exhaust_c: 41.2,
      load_1: 0.6,
      cpu_count: 6,
      mem_total_mb: 24000,
      mem_available_mb: 15200,
      disk_total_mb: 500000,
      disk_free_mb: 310000,
      uptime_s: 1_900_000,
      backup_configured: true,
      backup_ok_at: at(60 * 6),
      backup_age_h: 6,
      net: { tested_at: at(24), down_mbps: 94.2, up_mbps: 21.6, latency_ms: 18 },
      rx_bps: demoTraffic(now)[0],
      tx_bps: demoTraffic(now)[1],
      speed_series: [88, 91, 94, 79, 96, 90, 94],
    },
  ];
}
