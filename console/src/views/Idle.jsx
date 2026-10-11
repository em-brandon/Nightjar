import { useEffect, useState } from "react";
import { CircleX, Clapperboard, Inbox, FlaskConical, Rocket, TrendingUp, TriangleAlert } from "lucide-react";
import { ago, plural, tuning, useNow, useReveal } from "../hooks.ts";
import { Odometer, Pill, Sev, SiteCard, Spark } from "../ui.tsx";
import { requestsView } from "../home.ts";
import { label, mediaState } from "../media.ts";
import { DemoTag, DiskBar } from "./Media.jsx";
import PushCard from "../PushCard.tsx";
import { demoPush, pushView } from "../push.ts";
import StatusTray from "../StatusTray.jsx";
import WeatherLine from "../WeatherLine.tsx";
import { Verdict } from "./Overview.jsx";

const PER_PAGE = 2;
const ROTATE_MS = tuning("rotate", 120_000);
const REVEAL_MS = tuning("reveal", 120_000);
const TONE = { success: "ok", failed: "bad", running: "run" };

// Client requests are not a card: a request is a promise to a person, worth a nudge and not a
// panel. So it is an inbox icon and one number beside the incidents heading, with the words in the
// tooltip. Nothing shows while the inbox is clear. The number is how many are OVERDUE, turning the
// pill amber and letting it breathe; with none overdue it is how many are open, and stays dim.
function RequestsCue({ tickets }) {
  const r = requestsView(tickets);
  if (r.state === "clear") return null;
  const overdue = r.state === "overdue";
  const n = overdue ? r.overdue : r.open;
  return (
    <span
      className={`req-cue ${overdue ? "req-overdue" : "req-open"}`}
      title={overdue ? `${n} client request${n === 1 ? " is" : "s are"} past due` : `${n} open client request${n === 1 ? "" : "s"}`}
      aria-label={overdue ? `${n} overdue client requests` : `${n} open client requests`}
    >
      <Inbox size={15} strokeWidth={2} aria-hidden="true" />
      {n}
    </span>
  );
}

// Hidden detail is blurred, not replaced: the shape of a title stays, its words do not.
// aria-hidden keeps a screen reader from reading what the screen is hiding.
function Veil({ revealed, children }) {
  return (
    <span className={`veil ${revealed ? "" : "veiled"}`} aria-hidden={!revealed}>
      {children}
    </span>
  );
}

// A panel heading: icon and title in the panel's own colour.
function PanelTitle({ icon: Icon, children }) {
  return (
    <h2>
      <Icon size={18} strokeWidth={2} aria-hidden="true" />
      {children}
    </h2>
  );
}

const clock = (t) =>
  new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const mmss = (ms) => {
  const s = Math.ceil(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

// Tier 1 rule: hide detail, not names. Site, severity, count and age are always
// shown; the title (which can carry hostnames and identifiers) only when
// revealed.
function IncidentsPanel({ issues, tickets, blind, revealed, now }) {
  const open = issues.filter((i) => i.status === "open");
  const shown = open.slice(0, 5);
  return (
    <section className="card idle-panel idle-inc tone-yellow">
      <header>
        <PanelTitle icon={TriangleAlert}>Active incidents</PanelTitle>
        <RequestsCue tickets={tickets} />
        <span className="count"><Odometer value={open.length} /></span>
      </header>
      {open.length === 0 ? (
        // Silence must not read as all-clear: with the hub unreachable there is no list to be empty.
        blind ? <p className="idle-clear muted">Can't reach the hub</p> : <p className="idle-clear">All clear</p>
      ) : (
        <ul className="idle-list">
          {shown.map((i) => (
            <li key={i.id}>
              <Sev level={i.severity} />
              <span className="il-main">
                <span className="il-site">{i.site}</span>
                <Veil revealed={revealed}>
                  <span className="il-title">{i.title}</span>
                </Veil>
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
function DeployPanel({ title, icon, tone, deploys, revealed, now }) {
  return (
    <section className={`card idle-panel ${tone}`}>
      <header>
        <PanelTitle icon={icon}>{title}</PanelTitle>
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
                <Veil revealed={revealed}>
                  <span className="il-title">{d.ref || "—"}</span>
                </Veil>
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

// Errors rising across the client apps: an open issue whose events in the last 30 minutes are more
// than double the 30 before. It is a look-at-it list, not an alert. With nothing climbing it says so
// in words: an empty panel must not be mistaken for a panel that failed to load. The site and the
// counts are always shown; the error's title is detail, so it is blurred until the reveal.
function RisingPanel({ rising, blind, revealed }) {
  const climbing = rising.length > 0;
  return (
    <section className="card idle-panel tone-red">
      <header>
        <PanelTitle icon={climbing ? TrendingUp : CircleX}>Errors rising</PanelTitle>
        <span className="count"><Odometer value={rising.length} /></span>
      </header>
      {climbing ? (
        <ul className="idle-list">
          {rising.slice(0, 3).map((r) => (
            <li key={r.issue_id}>
              <span className="il-main">
                <span className="il-site">{r.site}</span>
                <Veil revealed={revealed}>
                  <span className="il-title">{r.title}</span>
                </Veil>
              </span>
              <Spark points={r.series} width={64} height={24} tone="var(--amber)" />
              <span className="il-side">
                <span>{r.recent}</span>
                <span>vs {r.previous} before</span>
              </span>
            </li>
          ))}
          {rising.length > 3 && <li className="il-more">+{rising.length - 3} more</li>}
        </ul>
      ) : (
        <p className="idle-clear idle-calm">{blind ? "Can't reach the hub" : "No errors rising"}</p>
      )}
      <p className="idle-note">last 30 min vs the 30 before, all client apps</p>
    </section>
  );
}

// Home media. The state, the number of streams and the disk are always shown;
// who is watching and what only after the reveal, like incident titles. A silent
// reporter reads as silent, never as an idle server.
const MEDIA_TEXT = { up: "up", down: "not responding", low: "disk nearly full", silent: "reporter silent" };
const MEDIA_TONE = { up: "ok", down: "bad", low: "warn", silent: "warn" };

function MediaPanel({ media, revealed, demo = false }) {
  const streams = media.flatMap((m) => (m.stale ? [] : m.sessions));
  return (
    <section className="card idle-panel tone-blue">
      <header>
        <PanelTitle icon={Clapperboard}>Home media</PanelTitle>
        {demo && <DemoTag />}
        <span className="count"><Odometer value={streams.length} /> streaming</span>
      </header>
      {media.map((m) => {
        const state = mediaState(m);
        return (
          <div key={m.site} className="idle-media">
            <div className="idle-media-head">
              <span className="il-site">{m.site}</span>
              <Pill tone={MEDIA_TONE[state]}>{MEDIA_TEXT[state]}</Pill>
            </div>
            <DiskBar m={m} />
          </div>
        );
      })}
      {demo && <p className="demo-note">Sample data. Connect the Jellyfin reporter to see your own.</p>}
      {streams.length > 0 && (
        <ul className="idle-list">
          {streams.slice(0, 3).map((s) => (
            <li key={s.session_id}>
              <span className="il-main">
                <Veil revealed={revealed}>
                  <span className="il-site">{s.user}</span>
                </Veil>
                <Veil revealed={revealed}>
                  <span className="il-title">{label(s)}</span>
                </Veil>
              </span>
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

  // The staging push card. A real push wins. Otherwise a sample plays, so the card can be judged: in
  // development always, and in a real build only until a staging deploy has ever been reported.
  const realPush = pushView(f.deploys, now, f.stacks);
  const showSample = f.loaded && !f.hubError && (import.meta.env.DEV || !f.deploys.some((d) => d.env === "staging"));
  const push = realPush ?? (showSample ? demoPush(now) : null);
  const pushIsSample = !realPush && showSample;
  const stagingDeploys = f.deploys.filter((d) => d.env === "staging");

  return (
    <div className="idle-screen">
      <header className="idle-top">
        <div className="brand">
          night<span>jar</span>
          <small>incident hub</small>
        </div>
        <StatusTray home={f.home} homeDemo={f.homeDemo} presence={f.presence} rates={f.rates} offline={f.hubError} now={now} />
        <div className="idle-right">
          <div className="idle-clock" aria-label="Time">
            <Odometer value={clock(now)} />
          </div>
          <WeatherLine weather={f.weather} />
        </div>
      </header>

      <div className="idle-verdict-row">
        <Verdict prod={prod} hubError={f.hubError} updatedAt={f.updatedAt} now={now} />
        <PushCard push={push} sample={pushIsSample} revealed={revealed} />
      </div>

      <div className="idle-grid">
        <div key={current} className="idle-sites">
          {visible.length === 0 ? (
            <p className="idle-clear muted">No production sites yet</p>
          ) : (
            visible.map((s) => <SiteCard key={s.id} site={s} now={now} big />)
          )}
        </div>
        <IncidentsPanel issues={f.issues} tickets={f.tickets} blind={f.hubError} revealed={revealed} now={now} />
        <DeployPanel title="Production deploys" icon={Rocket} tone="tone-orange" deploys={prodDeploys} revealed={revealed} now={now} />
        <DeployPanel title="Staging deploys" icon={FlaskConical} tone="tone-green" deploys={stagingDeploys} revealed={revealed} now={now} />
        <RisingPanel rising={f.rising} blind={f.hubError} revealed={revealed} />
        {f.media.length > 0 ? (
          <MediaPanel media={f.media} revealed={revealed} />
        ) : (
          f.mediaDemo && <MediaPanel media={f.mediaDemo} revealed={revealed} demo />
        )}
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
