import { ago, fmtPct, plural, send } from "../hooks.js";
import { readiness } from "../promotion.js";
import { unwatched } from "../monitoring.ts";
import { backupView, hostView, netView, presenceView } from "../home.js";
import { label, mediaState } from "../media.ts";
import { Dot, Empty, Odometer, Pill, Sev, SiteCard, Spark } from "../ui.jsx";
import { DemoTag, DiskBar } from "./Media.jsx";

const DEPLOY_TONE = { success: "ok", failed: "bad", running: "run" };
const DEPLOY_LABEL = { success: "success", failed: "failed", running: "running" };

const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

export function Verdict({ prod, hubError, updatedAt, now }) {
  const down = prod.filter((s) => s.state === "down").length;
  const warn = prod.filter((s) => s.state === "warn").length;
  // silence must not read as all-clear: a site nothing checks cannot be called quiet
  const blind = unwatched(prod).length;

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
  } else if (blind) {
    tone = "warn";
    headline = `${plural(blind, "site isn't", "sites aren't")} being checked`;
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

const MEDIA_TEXT = { up: "up", down: "not responding", low: "disk nearly full", silent: "reporter silent" };
const MEDIA_TONE = { up: "ok", down: "bad", low: "warn", silent: "warn" };

// Home media. Shows sample data, clearly tagged, until a real server is connected.
function MediaTile({ media, demo, go }) {
  const m = media[0];
  const state = mediaState(m);
  const streams = m.stale ? [] : m.sessions;
  return (
    <Tile title="Home media" to="media" go={go} tone={state === "down" ? "bad" : state === "up" ? "" : "warn"}>
      <div className="t-head">
        <span className="t-num">
          <Odometer value={streams.length} />
          <small> streaming</small>
        </span>
        <span className="t-chips">
          {demo && <DemoTag />}
          <Pill tone={MEDIA_TONE[state]}>{MEDIA_TEXT[state]}</Pill>
        </span>
      </div>
      {streams.length === 0 ? (
        <Empty>Nobody is watching right now.</Empty>
      ) : (
        <ul className="t-list">
          {streams.slice(0, 2).map((s) => (
            <li key={s.session_id}>
              <span className="t-main">
                <span>{s.user}</span>
                <span className="t-sub">{label(s)}</span>
              </span>
              {s.play_method === "transcode" && <Pill tone="warn">transcoding</Pill>}
            </li>
          ))}
        </ul>
      )}
      <DiskBar m={m} />
      {demo && <p className="demo-note">Sample data. Connect the Jellyfin reporter to see your own.</p>}
    </Tile>
  );
}

const HOME_TONE = { hot: "bad", low: "bad", warm: "warn", silent: "warn" };

// The home host: internet speed, the ThinkCentre's health, the backup, and who is home. Sample data,
// clearly tagged, until the home reporter is connected. The presence buttons are the console's own
// switch: an NFC tap is the normal way, and a tap that lapses falls back to away.
function HomeTile({ home, demo, presence, now, go, reloadPresence }) {
  const h = home[0];
  const net = netView(h, now);
  const host = hostView(h);
  const backup = backupView(h);
  const who = presenceView(presence);
  const set = (state) => async (e) => {
    e.stopPropagation(); // the tile itself is a link
    try {
      await send("POST", "/api/presence", { state, ttl_minutes: 840 });
      await reloadPresence();
    } catch {
      /* the next poll shows the truth */
    }
  };
  const tone = HOME_TONE[host?.state] ?? (backup && ["stale", "missing"].includes(backup.state) ? "warn" : "");
  return (
    <Tile title="Home network and host" to="overview" go={go} tone={tone}>
      <div className="t-head">
        <span className="t-num">
          {net && (net.state === "ok" || net.state === "old") ? (
            <>
              <Odometer value={net.down} />
              <small> Mbps down</small>
            </>
          ) : (
            <small>{net?.text ?? "no data"}</small>
          )}
        </span>
        <span className="t-chips">
          {demo && <DemoTag />}
          {who && (
            <span title={who.sub ?? undefined}>
              <Pill tone={who.state === "home" ? "ok" : "run"}>{who.text}</Pill>
            </span>
          )}
        </span>
      </div>
      {net?.series?.length > 1 && <Spark points={net.series} width={220} height={28} tone="var(--blue)" />}
      <ul className="t-list">
        <li>
          <span className="t-main">
            <span>Internet</span>
            <span className="t-sub">
              {net && (net.state === "ok" || net.state === "old")
                ? [net.up ? `↑ ${net.up} Mbps` : null, net.latency, `${net.age} ago`].filter(Boolean).join(" · ")
                : net?.text}
            </span>
          </span>
          {net?.state === "old" && <Pill tone="warn">test old</Pill>}
        </li>
        <li>
          <span className="t-main">
            <span>ThinkCentre</span>
            <span className="t-sub">
              {host?.state === "silent"
                ? "reporter silent"
                : [
                    host?.temp,
                    h?.intake_c != null ? `in ${Math.round(h.intake_c)}°` : null,
                    h?.exhaust_c != null ? `out ${Math.round(h.exhaust_c)}°` : null,
                    host?.diskFree != null ? `disk ${host.diskFree}% free` : null,
                    host?.memUsed != null ? `RAM ${host.memUsed}%` : null,
                  ]
                    .filter(Boolean)
                    .join(" · ") || "—"}
            </span>
          </span>
          {host && host.state !== "ok" && host.state !== "silent" && <Pill tone={host.state === "warm" ? "warn" : "bad"}>{host.state === "low" ? "disk low" : host.state}</Pill>}
        </li>
        <li>
          <span className="t-main">
            <span>Backup</span>
            <span className="t-sub">{backup?.text ?? "—"}</span>
          </span>
          {backup && ["stale", "missing"].includes(backup.state) && <Pill tone="warn">{backup.state}</Pill>}
        </li>
      </ul>
      <div className="home-actions">
        <button className="btn" onClick={set("home")}>I'm home</button>
        <button className="btn" onClick={set("away")}>Away</button>
        <span className="muted">home lasts 14 h</span>
      </div>
      {demo && <p className="demo-note">Sample data. Connect the home reporter to see your own.</p>}
    </Tile>
  );
}

export default function Overview({ f }) {
  const { sites, issues, rising, tickets, deploys, stacks, media, mediaDemo, home, homeDemo, presence, reloadPresence, now, go, hubError, updatedAt, loaded } = f;

  const prod = sites.filter((s) => s.env !== "staging");
  const staging = sites.filter((s) => s.env === "staging");
  const openIssues = issues.filter((i) => i.status === "open");
  const p1 = openIssues.filter((i) => i.severity === "P1").length;
  const p2 = openIssues.filter((i) => i.severity === "P2").length;
  const openTickets = tickets.filter((t) => t.status === "open");
  const overdue = openTickets.filter((t) => t.overdue);
  const prodDeploys = deploys.filter((d) => d.env === "production");
  const stagingDeploys = deploys.filter((d) => d.env === "staging");
  const verdicts = staging.map((s) => readiness({ site: s, sites, deploys, stacks, now }).verdict);
  const ready = verdicts.filter((v) => v === "ready").length;
  const blocked = verdicts.filter((v) => v === "blocked").length;

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
      {unwatched(prod).length > 0 && (
        <p className="unwatched" role="alert">
          Not being checked: <b>{unwatched(prod).join(", ")}</b>. Set a <code>url</code> with{" "}
          <code>PATCH /api/sites/&lt;id&gt;</code> to start uptime checks.
        </p>
      )}
      {prod.length === 0 ? (
        <Empty>No production sites yet. Create one with POST /api/sites, then give it a url to start checks.</Empty>
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
                <span className="t-chips">
                  {ready > 0 && <Pill tone="ok">{ready} ready to promote</Pill>}
                  {blocked > 0 && <Pill tone="bad">{blocked} not ready</Pill>}
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
        {(home.length > 0 || homeDemo) && (
          <HomeTile home={home.length > 0 ? home : homeDemo} demo={home.length === 0} presence={presence} now={now} go={go} reloadPresence={reloadPresence} />
        )}
        {(media.length > 0 || mediaDemo) && (
          <MediaTile media={media.length > 0 ? media : mediaDemo} demo={media.length === 0} go={go} />
        )}
      </div>
    </>
  );
}
