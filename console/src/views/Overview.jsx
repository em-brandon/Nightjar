import { ago, fmtPct, plural } from "../hooks.js";
import { Dot, Empty, Odometer, Pill, Sev, SiteCard, Spark } from "../ui.jsx";

const DEPLOY_TONE = { success: "ok", failed: "bad", running: "run" };
const DEPLOY_LABEL = { success: "success", failed: "failed", running: "running" };

const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

export function Verdict({ prod, hubError, updatedAt, now }) {
  const down = prod.filter((s) => s.state === "down").length;
  const warn = prod.filter((s) => s.state === "warn").length;

  let tone = "ok";
  let headline = "All quiet";
  if (hubError) {
    // Never show "All quiet" while blind. Silence must not read as all-clear.
    tone = "bad";
    headline = "Can't reach the hub";
  } else if (prod.length === 0) {
    tone = "idle";
    headline = "No sites yet";
  } else if (down) {
    tone = "bad";
    headline = `${plural(down, "site is", "sites are")} down`;
  } else if (warn) {
    tone = "warn";
    headline = `${plural(warn, "site needs", "sites need")} a look`;
  }

  let sub = `${plural(prod.length, "production site", "production sites")} · updated ${ago(updatedAt, now)}`;
  if (hubError) {
    sub = updatedAt
      ? `showing the last good data from ${ago(updatedAt, now)}`
      : "no data received yet · retrying every 30s";
  }
  return (
    <div className="verdict">
      <div className={`v-glow ${tone}`} aria-hidden="true" />
      <h1 className={tone}>{headline}</h1>
      <p>{sub}</p>
    </div>
  );
}

function Stat({ label, sub, tone = "", children }) {
  return (
    <div className={`card stat ${tone}`}>
      <span className="k">{label}</span>
      <span className="big">{children}</span>
      <span className="sub">{sub}</span>
    </div>
  );
}

// A whole tile is the link to its tab. A div with a role, not a button, because
// tiles contain lists and a button cannot legally contain interactive content.
function Tile({ title, to, go, children, tone = "" }) {
  return (
    <article
      className={`card tile ${tone}`}
      role="link"
      tabIndex={0}
      onClick={() => go(to)}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), go(to))}
    >
      <header>
        <h2>{title}</h2>
        <span className="open">open →</span>
      </header>
      {children}
    </article>
  );
}

export default function Overview({ f }) {
  const { sites, issues, rising, tickets, deploys, now, go, hubError, updatedAt, loaded } = f;

  const prod = sites.filter((s) => s.env !== "staging");
  const staging = sites.filter((s) => s.env === "staging");
  const openIssues = issues.filter((i) => i.status === "open");
  const p1 = openIssues.filter((i) => i.severity === "P1").length;
  const p2 = openIssues.filter((i) => i.severity === "P2").length;
  const openTickets = tickets.filter((t) => t.status === "open");
  const overdue = openTickets.filter((t) => t.overdue);
  const prodDeploys = deploys.filter((d) => d.env === "production");
  const stagingDeploys = deploys.filter((d) => d.env === "staging");

  const monitored = prod.filter((s) => s.monitored);
  const upCount = prod.filter((s) => s.state === "up").length;
  const uptime = mean(monitored.map((s) => s.uptime_24h).filter((x) => x != null));
  const latency = mean(monitored.map((s) => s.latency_avg_ms).filter((x) => x != null));

  if (!loaded && !hubError) return <p className="loading">Loading…</p>;

  return (
    <>
      <Verdict prod={prod} hubError={hubError} updatedAt={updatedAt} now={now} />

      <section className="stats-strip" aria-label="Summary">
        <Stat
          label="Sites up"
          sub={prod.length ? `of ${prod.length} production` : "none yet"}
          tone={prod.length && upCount < prod.length ? "bad" : ""}
        >
          <Odometer value={upCount} />
        </Stat>
        <Stat label="Uptime · 24h" sub={monitored.length ? "mean across checks" : "no checks yet"}>
          <Odometer value={fmtPct(uptime)} />
          {uptime != null && <small>%</small>}
        </Stat>
        <Stat label="Latency" sub="mean, 24h">
          <Odometer value={latency == null ? "—" : Math.round(latency)} />
          {latency != null && <small>ms</small>}
        </Stat>
        <Stat label="Open incidents" sub={p1 ? `${p1} at P1` : p2 ? `${p2} at P2` : "none"} tone={p1 ? "bad" : ""}>
          <Odometer value={openIssues.length} />
        </Stat>
        <Stat
          label="Client requests"
          sub={overdue.length ? `${overdue.length} overdue` : "open"}
          tone={overdue.length ? "warn" : ""}
        >
          <Odometer value={openTickets.length} />
        </Stat>
      </section>

      <div className="sec">
        <h2>Site checks</h2>
        <span className="count">production · probed every minute</span>
      </div>
      {prod.length === 0 ? (
        <Empty>No production sites yet. Run seed.py, then set a url on the site to start checks.</Empty>
      ) : (
        <div className="site-grid">
          {prod.map((s) => (
            <SiteCard key={s.id} site={s} now={now} />
          ))}
        </div>
      )}

      <div className="sec">
        <h2>At a glance</h2>
        <span className="count">click a tile for the full view</span>
      </div>
      <div className="tiles">
        <Tile title="Incidents" to="incidents" go={go} tone={p1 ? "bad" : ""}>
          <div className="t-head">
            <span className="t-num"><Odometer value={openIssues.length} /></span>
            <span className="t-chips">
              {p1 > 0 && <Sev level="P1" />}
              {p1 > 0 && <b>{p1}</b>}
              {p2 > 0 && <Sev level="P2" />}
              {p2 > 0 && <b>{p2}</b>}
              {openIssues.length === 0 && <span className="muted">all clear</span>}
            </span>
          </div>
          <ul className="t-list">
            {openIssues.slice(0, 3).map((i) => (
              <li key={i.id}>
                <Sev level={i.severity} />
                <span className="t-main">
                  <span>{i.site}</span>
                  <span className="t-sub">{i.title}</span>
                </span>
                <span className={`t-side ${i.acknowledged ? "" : "c-p1"}`}>
                  {i.acknowledged ? "acked" : "unacked"}
                </span>
              </li>
            ))}
          </ul>
        </Tile>

        <Tile title="Errors rising" to="incidents" go={go} tone={rising.length ? "warn" : ""}>
          <div className="t-head">
            <span className="t-num"><Odometer value={rising.length} /></span>
            <span className="t-chips muted">last 30 min vs before</span>
          </div>
          {rising.length === 0 ? (
            <Empty>Nothing climbing.</Empty>
          ) : (
            <ul className="t-list">
              {rising.slice(0, 3).map((r) => (
                <li key={r.issue_id}>
                  <span className="t-main">
                    <span>{r.site}</span>
                    <span className="t-sub">{r.title}</span>
                  </span>
                  <Spark points={r.series} width={72} height={24} tone="var(--amber)" />
                  <span className="t-side c-p2">
                    {r.recent}
                    <small> vs {r.previous}</small>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Tile>

        <Tile title="Client requests" to="requests" go={go} tone={overdue.length ? "warn" : ""}>
          <div className="t-head">
            <span className="t-num"><Odometer value={openTickets.length} /></span>
            <span className="t-chips">
              {overdue.length > 0 ? (
                <Pill tone="bad">{overdue.length} overdue</Pill>
              ) : (
                <span className="muted">{openTickets.length ? "none overdue" : "inbox clear"}</span>
              )}
            </span>
          </div>
          <ul className="t-list">
            {openTickets.slice(0, 3).map((t) => (
              <li key={t.id}>
                <span className={`kind ${t.kind}`}>{t.kind}</span>
                <span className="t-main">
                  <span>{t.subject}</span>
                  <span className="t-sub">{t.site ?? "no site"}</span>
                </span>
                <span className={`t-side ${t.overdue ? "c-p1" : ""}`}>{ago(t.created_at, now)}</span>
              </li>
            ))}
          </ul>
        </Tile>

        <Tile title="Production deploys" to="deploys" go={go} tone={prodDeploys[0]?.status === "failed" ? "bad" : ""}>
          {prodDeploys.length === 0 ? (
            <Empty>None reported yet. CI reports each deploy to the hub.</Empty>
          ) : (
            <>
              <div className="t-head">
                <Pill tone={DEPLOY_TONE[prodDeploys[0].status]}>{DEPLOY_LABEL[prodDeploys[0].status]}</Pill>
                <span className="t-chips muted">
                  {prodDeploys[0].site} · {ago(prodDeploys[0].created_at, now)}
                </span>
              </div>
              <ul className="t-list">
                {prodDeploys.slice(0, 3).map((d) => (
                  <li key={d.id}>
                    <span className="t-main">
                      <span>{d.site}</span>
                      <span className="t-sub mono">{d.ref || "—"}</span>
                    </span>
                    <Pill tone={DEPLOY_TONE[d.status]}>{DEPLOY_LABEL[d.status]}</Pill>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Tile>

        <Tile title="Staging" to="staging" go={go} tone={staging.some((s) => s.state === "down") ? "bad" : ""}>
          {staging.length === 0 ? (
            <Empty>No staging site yet. Add a site with env staging and a url.</Empty>
          ) : (
            <>
              <div className="t-head">
                <span className="t-num">
                  <Odometer value={staging.filter((s) => s.state === "up").length} />
                  <small> / {staging.length} up</small>
                </span>
              </div>
              <ul className="t-list">
                {staging.slice(0, 3).map((s) => (
                  <li key={s.id}>
                    <Dot state={s.state} />
                    <span className="t-main">
                      <span>{s.name}</span>
                      <span className="t-sub">
                        {s.monitored ? `${fmtPct(s.uptime_24h)}% · ${s.latency_ms ?? "—"} ms` : "not monitored"}
                      </span>
                    </span>
                    {(() => {
                      const d = stagingDeploys.find((x) => x.site === s.name);
                      return d ? <Pill tone={DEPLOY_TONE[d.status]}>{DEPLOY_LABEL[d.status]}</Pill> : null;
                    })()}
                  </li>
                ))}
              </ul>
            </>
          )}
        </Tile>
      </div>
    </>
  );
}
