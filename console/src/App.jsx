import Splash from "./Splash.jsx";
import { ago, settled, useHashTab, useNow, usePoll } from "./hooks.js";
import Deploys from "./views/Deploys.jsx";
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
const TAB_IDS = TABS.map(([id]) => id);
const VIEWS = { overview: Overview, incidents: Incidents, requests: Requests, deploys: Deploys, staging: Staging };

export default function App() {
  const health = usePoll("/api/sites/health");
  const issues = usePoll("/api/issues?status=all");
  const rising = usePoll("/api/errors/rising");
  const tickets = usePoll("/api/tickets?status=all");
  const deploys = usePoll("/api/deploys");
  const now = useNow();
  const [tab, go] = useHashTab(TAB_IDS, "overview");

  const feeds = [health, issues, rising, tickets, deploys];
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
    now,
    go,
    hubError,
    loaded,
    updatedAt,
    reloadIssues: () => Promise.all([issues.reload(), health.reload(), rising.reload()]),
    reloadTickets: tickets.reload,
  };

  const openIssues = f.issues.filter((i) => i.status === "open");
  const openTickets = f.tickets.filter((t) => t.status === "open");
  const badges = {
    incidents: { n: openIssues.length, hot: openIssues.some((i) => i.severity === "P1") },
    requests: { n: openTickets.length, hot: openTickets.some((t) => t.overdue) },
  };

  const View = VIEWS[tab];
  return (
    <div className="shell">
      <Splash />
      <header className="top">
        <div className="brand">
          night<span>jar</span>
        </div>
        <nav className="tabs" aria-label="Sections">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              className={`tab ${tab === id ? "on" : ""}`}
              aria-current={tab === id ? "page" : undefined}
              onClick={() => go(id)}
            >
              {label}
              {badges[id]?.n > 0 && <span className={`badge ${badges[id].hot ? "hot" : ""}`}>{badges[id].n}</span>}
            </button>
          ))}
        </nav>
        <div className="updated">{updatedAt ? `updated ${ago(updatedAt, now)}` : ""}</div>
      </header>

      {hubError && tab !== "overview" && (
        <div className="banner" role="alert">
          Can't reach the hub. This page may be out of date
          {updatedAt ? ` (last good data ${ago(updatedAt, now)})` : ""}.
        </div>
      )}

      <main>{loaded || hubError ? <View f={f} /> : <p className="loading">Loading…</p>}</main>
    </div>
  );
}
