import Splash from "./Splash.jsx";
import { Dot } from "./ui.jsx";
import { ago, settled, tuning, useHashTab, useInactivity, useNow, usePoll } from "./hooks.js";
import Deploys from "./views/Deploys.jsx";
import Idle from "./views/Idle.jsx";
import Incidents from "./views/Incidents.jsx";
import Overview from "./views/Overview.jsx";
import Requests from "./views/Requests.jsx";
import Staging from "./views/Staging.jsx";

const TABS = [
  ["overview", "Overview"],
  ["incidents", "Incidents"],
  ["requests", "Requests"],
  ["deploys", "Deploys"],
  ["staging", "Staging"],
];
// "idle" is a screen, not a tab: it is reachable at #/idle and starts by itself
// after IDLE_AFTER_MS with no input, but has no sidebar entry.
const TAB_IDS = [...TABS.map(([id]) => id), "idle"];
const IDLE_AFTER_MS = tuning("idle", 5 * 60_000);

// Simple stroke icons, 18x18. Inline so there is no icon dependency.
const ICONS = {
  overview: <path d="M3 3h5v5H3zM10 3h5v5h-5zM3 10h5v5H3zM10 10h5v5h-5z" />,
  incidents: <path d="M9 2.5 16 15H2zM9 7.5v3.5M9 13v.01" />,
  requests: <path d="M2.5 10.5 4.5 4h9l2 6.5M2.5 10.5v4h13v-4M2.5 10.5H6l1 1.5h4l1-1.5h3.5" />,
  deploys: <path d="M9 15V4M4.5 8.5 9 4l4.5 4.5M3 15.5h12" />,
  staging: <path d="M9 2 16 6 9 10 2 6zM2 9.5 9 13.5 16 9.5M2 12.5 9 16.5 16 12.5" />,
};
const VIEWS = { overview: Overview, incidents: Incidents, requests: Requests, deploys: Deploys, staging: Staging };

export default function App() {
  const health = usePoll("/api/sites/health");
  const issues = usePoll("/api/issues?status=all");
  const rising = usePoll("/api/errors/rising");
  const tickets = usePoll("/api/tickets?status=all");
  const deploys = usePoll("/api/deploys");
  const stacks = usePoll("/api/stacks");
  const now = useNow();
  const [tab, go] = useHashTab(TAB_IDS, "overview");
  useInactivity(IDLE_AFTER_MS, () => go("idle"), tab !== "idle");

  const feeds = [health, issues, rising, tickets, deploys, stacks];
  const hubError = feeds.some((x) => x.error);
  const loaded = feeds.every((x) => x.data);
  const stamps = feeds.map((x) => x.at).filter(Boolean);
  const updatedAt = stamps.length === feeds.length ? Math.min(...stamps) : null;

  const f = {
    sites: health.data ?? [],
    issues: issues.data ?? [],
    rising: rising.data ?? [],
    tickets: tickets.data ?? [],
    deploys: settled(deploys.data ?? []),
    stacks: stacks.data ?? [],
    now,
    go,
    hubError,
    loaded,
    updatedAt,
    reloadIssues: () => Promise.all([issues.reload(), health.reload(), rising.reload()]),
    reloadTickets: tickets.reload,
    reloadStacks: stacks.reload,
  };

  const openIssues = f.issues.filter((i) => i.status === "open");
  const openTickets = f.tickets.filter((t) => t.status === "open");
  const badges = {
    incidents: { n: openIssues.length, hot: openIssues.some((i) => i.severity === "P1") },
    requests: { n: openTickets.length, hot: openTickets.some((t) => t.overdue) },
  };

  // The idle screen owns the whole viewport (no sidebar). It is left only by
  // revealing detail and choosing Unlock, never by stray mouse movement.
  if (tab === "idle") {
    return (
      <>
        <Splash />
        <Idle f={f} go={go} />
      </>
    );
  }

  const View = VIEWS[tab];
  return (
    <div className="app">
      <Splash />
      <aside className="side">
        <div className="brand">
          night<span>jar</span>
          <small>incident hub</small>
        </div>
        <nav className="nav" aria-label="Sections">
          <div className="nav-label">Console</div>
          {TABS.map(([id, label]) => (
            <button
              key={id}
              className={`tab ${tab === id ? "on" : ""}`}
              aria-current={tab === id ? "page" : undefined}
              onClick={() => go(id)}
            >
              <svg viewBox="0 0 18 18" aria-hidden="true">{ICONS[id]}</svg>
              {label}
              {badges[id]?.n > 0 && <span className={`badge ${badges[id].hot ? "hot" : ""}`}>{badges[id].n}</span>}
            </button>
          ))}
        </nav>
        <div className="side-foot">
          <span className={`conn ${hubError ? "bad" : ""}`}>
            <Dot state={hubError ? "down" : "up"} />
            {hubError ? "hub unreachable" : "hub connected"}
          </span>
          <span>{updatedAt ? `updated ${ago(updatedAt, now)}` : "waiting for data"}</span>
          <button className="side-link" onClick={() => go("idle")}>Show idle screen</button>
        </div>
      </aside>

      <div className="main">
        {hubError && tab !== "overview" && (
          <div className="banner" role="alert">
            Can't reach the hub. This page may be out of date
            {updatedAt ? ` (last good data ${ago(updatedAt, now)})` : ""}.
          </div>
        )}

        {loaded || hubError ? <View f={f} /> : <p className="loading">Loading…</p>}
      </div>
    </div>
  );
}
