import { useEffect, useState } from "react";
import { ago, plural, tuning, useNow, useReveal } from "../hooks.js";
import { Odometer, Pill, Sev, SiteCard } from "../ui.jsx";
import { Verdict } from "./Overview.jsx";

const PER_PAGE = 2;
const ROTATE_MS = tuning("rotate", 120_000);
const REVEAL_MS = tuning("reveal", 120_000);
const TONE = { success: "ok", failed: "bad", running: "run" };
const HIDDEN = "•••••••";

const clock = (t) =>
  new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
const mmss = (ms) => {
  const s = Math.ceil(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

// Tier 1 rule: hide detail, not names. Site, severity, count and age are always
// shown; the title (which can carry hostnames and identifiers) only when
// revealed.
function IncidentsPanel({ issues, revealed, now }) {
  const open = issues.filter((i) => i.status === "open");
  const shown = open.slice(0, 5);
  return (
    <section className="card idle-panel idle-inc">
      <header>
        <h2>Active incidents</h2>
        <span className="count"><Odometer value={open.length} /></span>
      </header>
      {open.length === 0 ? (
        <p className="idle-clear">All clear</p>
      ) : (
        <ul className="idle-list">
          {shown.map((i) => (
            <li key={i.id}>
              <Sev level={i.severity} />
              <span className="il-main">
                <span className="il-site">{i.site}</span>
                {revealed && <span className="il-title">{i.title}</span>}
              </span>
              <span className="il-side">
                <span>×{i.event_count.toLocaleString()}</span>
                <span className={i.acknowledged ? "" : "c-p1"}>
                  {i.acknowledged ? "acked" : "unacked"} · {ago(i.last_seen, now)}
                </span>
              </span>
            </li>
          ))}
          {open.length > shown.length && (
            <li className="il-more">+{open.length - shown.length} more</li>
          )}
        </ul>
      )}
    </section>
  );
}

// Commit refs are detail (decision #13), so they are hidden until revealed.
function DeployPanel({ title, deploys, revealed, now }) {
  return (
    <section className="card idle-panel">
      <header>
        <h2>{title}</h2>
        <span className="count">{plural(deploys.length, "deploy", "deploys")}</span>
      </header>
      {deploys.length === 0 ? (
        <p className="idle-clear muted">None reported</p>
      ) : (
        <ul className="idle-list">
          {deploys.slice(0, 3).map((d) => (
            <li key={d.id}>
              <span className="il-main">
                <span className="il-site">{d.site}</span>
                <span className="il-title">{revealed ? d.ref || "—" : HIDDEN}</span>
              </span>
              <Pill tone={TONE[d.status]}>{d.status}</Pill>
              <span className="il-age">{ago(d.created_at, now)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function Idle({ f, go }) {
  const now = useNow(1000);
  const { revealed, remainingMs, reveal, hide } = useReveal(REVEAL_MS, now);

  const prod = f.sites.filter((s) => s.env !== "staging");
  const pages = Math.max(1, Math.ceil(prod.length / PER_PAGE));
  const [page, setPage] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const current = page % pages;

  // Advance every ROTATE_MS. Ticks with the shared 1s clock, so the countdown
  // and the rotation cannot drift apart. Nothing rotates with one page.
  useEffect(() => {
    if (pages <= 1) return;
    setElapsed((e) => e + 1000);
  }, [now, pages]);
  useEffect(() => {
    if (elapsed >= ROTATE_MS) {
      setPage((p) => (p + 1) % pages);
      setElapsed(0);
    }
  }, [elapsed, pages]);
  // The site list changed underneath us (a site was added or removed): restart
  // from the first page rather than landing on a page that no longer exists.
  useEffect(() => {
    setPage(0);
    setElapsed(0);
  }, [pages]);

  const visible = prod.slice(current * PER_PAGE, current * PER_PAGE + PER_PAGE);
  const prodDeploys = f.deploys.filter((d) => d.env === "production");
  const stagingDeploys = f.deploys.filter((d) => d.env === "staging");

  return (
    <div className="idle">
      <header className="idle-top">
        <div className="brand">
          night<span>jar</span>
          <small>incident hub</small>
        </div>
        <div className="idle-clock" aria-label="Time">
          <Odometer value={clock(now)} />
        </div>
      </header>

      <Verdict prod={prod} hubError={f.hubError} updatedAt={f.updatedAt} now={now} />

      <div className="idle-grid">
        <div key={current} className="idle-sites">
          {visible.length === 0 ? (
            <p className="idle-clear muted">No production sites yet</p>
          ) : (
            visible.map((s) => <SiteCard key={s.id} site={s} now={now} big />)
          )}
        </div>
        <IncidentsPanel issues={f.issues} revealed={revealed} now={now} />
        <DeployPanel title="Production deploys" deploys={prodDeploys} revealed={revealed} now={now} />
        <DeployPanel title="Staging deploys" deploys={stagingDeploys} revealed={revealed} now={now} />
      </div>

      <footer className="idle-foot">
        <div className="carousel" aria-label="Site pages">
          {pages > 1 && (
            <>
              {Array.from({ length: pages }, (_, i) => (
                <i key={i} className={i === current ? "on" : ""} />
              ))}
              <span className="muted">next in {mmss(ROTATE_MS - elapsed)}</span>
            </>
          )}
        </div>

        {revealed ? (
          <div className="reveal-bar" role="status">
            <span>Details shown · hides in {mmss(remainingMs)}</span>
            <button className="btn" onClick={hide}>Hide now</button>
            <button className="btn pri" onClick={() => go("overview")}>Unlock console</button>
          </div>
        ) : (
          <button className="btn ghost" onClick={reveal} title="Stand-in until the fingerprint reader is connected">
            Simulate fingerprint
            <small>reader not connected</small>
          </button>
        )}
      </footer>
    </div>
  );
}
