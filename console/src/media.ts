// Home media (Jellyfin) helpers. Pure functions, so the judgement calls can be
// tested without a browser: see media.test.mjs.

import type { components } from "./api/schema.d.ts";
import type { MediaPlay, MediaSnapshot } from "./api/types.ts";

type Session = components["schemas"]["MediaSessionIn"];

// Sample data is a snapshot that says so. The flag is how every place that draws it keeps it out of
// counts, verdicts and alerts (see demoMedia).
export type DemoMedia = MediaSnapshot & { demo: true };

// What one server is doing, worst first. A silent reporter is never "up": silence
// looks exactly like an idle server, so it has to be its own state.
export function mediaState(m: Pick<MediaSnapshot, "stale" | "up" | "host_disk_total_mb" | "host_disk_free_mb">): "silent" | "down" | "low" | "up" {
  if (m.stale) return "silent";
  if (!m.up) return "down";
  const free = diskFreePct(m);
  if (free != null && free < 10) return "low";
  return "up";
}

export function diskFreePct(m: Pick<MediaSnapshot, "host_disk_total_mb" | "host_disk_free_mb">): number | null {
  if (!m.host_disk_total_mb || m.host_disk_free_mb == null) return null;
  return (m.host_disk_free_mb / m.host_disk_total_mb) * 100;
}

// "S03E04 · Title" for an episode, the title alone for anything else.
export function label(s: Pick<Session, "series" | "episode" | "title">): string {
  if (s.series) return `${s.series}${s.episode ? ` ${s.episode}` : ""}: ${s.title}`;
  return s.title;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const m = Math.round(seconds / 60);
  if (m < 1) return "<1m";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}

export function progress(s: Pick<Session, "runtime_s" | "position_s">): number | null {
  if (!s.runtime_s || s.position_s == null) return null;
  return Math.max(0, Math.min(100, (s.position_s / s.runtime_s) * 100));
}

// Plays started in the last `days` days.
export function within(plays: MediaPlay[], days: number, now: number): MediaPlay[] {
  const cutoff = now - days * 86_400_000;
  return plays.filter((p) => new Date(p.started_at).getTime() >= cutoff);
}

// Per person: how many viewings and how long, most watched first. Uses
// `watched_s` (paused time and reporter gaps already excluded by the hub).
export function byUser(plays: Pick<MediaPlay, "user" | "watched_s" | "started_at">[]): { user: string; plays: number; watched_s: number; last: string }[] {
  const users = new Map<string, { user: string; plays: number; watched_s: number; last: string }>();
  for (const p of plays) {
    const u = users.get(p.user) ?? { user: p.user, plays: 0, watched_s: 0, last: p.started_at };
    u.plays += 1;
    u.watched_s += p.watched_s;
    if (p.started_at > u.last) u.last = p.started_at;
    users.set(p.user, u);
  }
  return [...users.values()].sort((a, b) => b.watched_s - a.watched_s || a.user.localeCompare(b.user));
}

// The most watched shows: a series counts as one entry however many episodes.
export function topTitles(plays: Pick<MediaPlay, "series" | "title" | "watched_s">[], limit = 5): { name: string; plays: number; watched_s: number }[] {
  const titles = new Map<string, { name: string; plays: number; watched_s: number }>();
  for (const p of plays) {
    const name = p.series || p.title;
    const t = titles.get(name) ?? { name, plays: 0, watched_s: 0 };
    t.plays += 1;
    t.watched_s += p.watched_s;
    titles.set(name, t);
  }
  return [...titles.values()].sort((a, b) => b.watched_s - a.watched_s || a.name.localeCompare(b.name)).slice(0, limit);
}

// Sample data for the dashboard and the idle screen while no real media server is
// connected, so the operator can see how the panels will look. It is flagged `demo`
// everywhere it is drawn and never enters any count, verdict or alert: showing
// invented streams as if they were real would be worse than showing nothing.
export function demoMedia(now: number = Date.now()): DemoMedia[] {
  const base = { kind: "Episode", paused: false, runtime_s: 1320 };
  return [
    {
      site: "home-media",
      demo: true,
      stale: false,
      up: true,
      version: null,
      reported_at: new Date(now).toISOString(),
      host_disk_total_mb: 2_000_000,
      host_disk_free_mb: 1_100_000,
      sessions: [
        { ...base, session_id: "demo1", user: "Amani", item_id: "d1", title: "The One with the Pilot", series: "Friends", episode: "S01E01", client: "Jellyfin Roku", device: "Living room TV", play_method: "direct_play", position_s: 640 },
        { ...base, session_id: "demo2", user: "Baraka", item_id: "d2", title: "Halloween", series: "The Office", episode: "S02E05", client: "Jellyfin Android TV", device: "Bedroom", play_method: "transcode", position_s: 300, runtime_s: 1260 },
      ],
    },
  ];
}
