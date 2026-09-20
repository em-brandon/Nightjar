import { useCallback, useEffect, useRef, useState } from "react";
import Splash from "./Splash.jsx";

const POLL_MS = 30_000;

// ---------------------------------------------------------------- helpers

function ago(iso, now) {
  if (!iso) return "never";
  const s = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const fmtCount = (n) => `×${n.toLocaleString()}`;
const fmtTime = (iso) => new Date(iso).toLocaleString();
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// ---------------------------------------------------------------- hooks

// Fetch on mount and every `ms`. On failure the last good data is kept and
// `error` is set: a blank page during a hub restart would be worse than a
// slightly stale one, as long as the page says plainly that it is stale.
function usePoll(url, ms) {
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

  return state;
}

// Re-render every few seconds so "12s ago" labels do not freeze between polls.
function useNow(ms = 5000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

// ---------------------------------------------------------------- pieces

function Verdict({ sites, hubError, updatedAt, now }) {
  const down = sites.filter((s) => s.state === "down").length;
  const warn = sites.filter((s) => s.state === "warn").length;

  let tone = "ok";
  let headline = "All quiet";
  if (hubError) {
    // Never show "All quiet" while blind. Silence must not read as all-clear.
    tone = "bad";
    headline = "Can't reach the hub";
  } else if (sites.length === 0) {
    tone = "idle";
    headline = "No sites yet";
  } else if (down) {
    tone = "bad";
    headline = `${plural(down, "site is", "sites are")} down`;
  } else if (warn) {
    tone = "warn";
    headline = `${plural(warn, "site needs", "sites need")} a look`;
  }

  let sub = `${plural(sites.length, "site", "sites")} · updated ${ago(updatedAt, now)}`;
  if (hubError) {
    sub = updatedAt
      ? `showing the last good data from ${ago(updatedAt, now)}`
      : "no data received yet · retrying every 30s";
  }

  return (
    <header className="verdict">
      <h1 className={tone}>{headline}</h1>
      <p>{sub}</p>
    </header>
  );
}

function SiteTile({ site, now }) {
  const total = site.open_p1 + site.open_p2 + site.open_p3;
  return (
    <div className="site">
      <div className="site-name">
        <span className={`dot ${site.state}`} aria-hidden="true" />
        {site.name}
        <span className="env">{site.env}</span>
      </div>
      <div className="site-meta">
        {total === 0 ? (
          <b>no open issues</b>
        ) : (
          <>
            <b>{plural(total, "open issue", "open issues")}</b>
            {site.open_p1 > 0 && <span className="c-p1"> · {site.open_p1} P1</span>}
            {site.open_p2 > 0 && <span className="c-p2"> · {site.open_p2} P2</span>}
            {site.open_p3 > 0 && <span> · {site.open_p3} P3</span>}
          </>
        )}
      </div>
      <div className="site-meta dim">last event {ago(site.last_event, now)}</div>
    </div>
  );
}

function IssueRow({ issue, now, onOpen }) {
  const sev = issue.severity.toLowerCase();
  return (
    <button className="issue" onClick={() => onOpen(issue.id)}>
      <span className={`sev ${sev}`}>{issue.severity}</span>
      <span className="issue-main">
        <span className="who">
          {issue.site}
          <span className="env">{issue.env}</span>
        </span>
        <span className="detail">{issue.title}</span>
      </span>
      <span className="right">
        <span className="n">{fmtCount(issue.event_count)}</span>
        <span className="ago">{ago(issue.last_seen, now)}</span>
      </span>
    </button>
  );
}

function Drawer({ issue, now, onClose }) {
  const closeRef = useRef(null);

  useEffect(() => {
    const prev = document.activeElement;
    closeRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", onKey);
    return () => {
      removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [onClose]);

  const sev = issue.severity.toLowerCase();
  const stats = [
    ["events", issue.event_count.toLocaleString()],
    ["first seen", ago(issue.first_seen, now)],
    ["last seen", ago(issue.last_seen, now)],
  ];

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Issue detail">
        <div className="hd">
          <span className={`sev ${sev}`}>{issue.severity}</span>
          <div>
            <h2>{issue.title}</h2>
            <div className="sub">
              {issue.site} · {issue.env}
            </div>
          </div>
          <button ref={closeRef} className="close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>

        <div className="stats">
          {stats.map(([k, v]) => (
            <div className="stat" key={k}>
              <div className="k">{k}</div>
              <div className="v">{v}</div>
            </div>
          ))}
        </div>

        <dl className="facts">
          <dt>culprit</dt>
          <dd>{issue.culprit || "—"}</dd>
          <dt>fingerprint</dt>
          <dd>{issue.fingerprint}</dd>
          <dt>first seen</dt>
          <dd>{fmtTime(issue.first_seen)}</dd>
          <dt>last seen</dt>
          <dd>{fmtTime(issue.last_seen)}</dd>
        </dl>
      </aside>
    </>
  );
}

// ---------------------------------------------------------------- page

export default function App() {
  const health = usePoll("/api/sites/health", POLL_MS);
  const issues = usePoll("/api/issues", POLL_MS);
  const now = useNow();
  const [openId, setOpenId] = useState(null);
  const close = useCallback(() => setOpenId(null), []);

  const sites = health.data ?? [];
  const list = issues.data ?? [];
  const hubError = health.error || issues.error;
  const loaded = health.data && issues.data;
  const updatedAt = Math.min(health.at ?? Infinity, issues.at ?? Infinity);

  // Look the issue up in live data so the drawer refreshes with each poll,
  // and closes by itself if the issue is resolved while it is open.
  const selected = list.find((i) => i.id === openId);

  return (
    <div className="shell">
      <Splash />
      <div className="top">
        <div className="brand">
          night<span>jar</span>
        </div>
        <div className="sub-brand">incident hub</div>
      </div>

      {!loaded && !hubError ? (
        <p className="loading">Loading…</p>
      ) : (
        <>
          <Verdict
            sites={sites}
            hubError={hubError}
            updatedAt={Number.isFinite(updatedAt) ? updatedAt : null}
            now={now}
          />

          {sites.length > 0 && (
            <div className="sites">
              {sites.map((s) => (
                <SiteTile key={s.id} site={s} now={now} />
              ))}
            </div>
          )}

          <div className="sec">
            <h2>Open issues</h2>
            {loaded && <span className="count">{list.length}</span>}
          </div>
          {list.length === 0 ? (
            <p className="empty">
              {loaded ? "Nothing open. Enjoy it." : "No data yet."}
            </p>
          ) : (
            <div className="issues">
              {list.map((i) => (
                <IssueRow key={i.id} issue={i} now={now} onOpen={setOpenId} />
              ))}
            </div>
          )}
        </>
      )}

      {selected && <Drawer issue={selected} now={now} onClose={close} />}
    </div>
  );
}
