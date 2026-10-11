import { useState } from "react";
import { ago, plural, send } from "../hooks.ts";
import { Dot, Odometer, Pill, SiteCard, Spark } from "../ui.tsx";
import { readiness, runningBuilds } from "../promotion.ts";
import { DeployHowTo, DeployList } from "./Deploys.tsx";

// Where "Manage stacks" points. Start, stop and logs live in Dockge or Portainer
// on the staging host, reached over Tailscale: Nightjar only shows stacks, and
// is never given the power to control them.
const CONTROL_URL = import.meta.env.VITE_STACK_CONTROL_URL;

const STATE_DOT = { running: "up", degraded: "warn", stopped: "idle" };
const TONE = { running: "ok", degraded: "warn", stopped: "run" };
const DEPLOY_TONE = { success: "ok", failed: "bad", running: "run" };

// Docker reports binary units, so label them as such.
const gb = (mb) => `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GiB`;
const mem = (mb) => (mb == null ? "—" : mb >= 1024 ? gb(mb) : `${mb} MiB`);

// Memory against a limit. With no limit the bar is drawn dashed against the
// whole host and labelled, because one runaway app can then take the machine.
function MemBar({ used, limit, host }) {
  const total = limit ?? host;
  const pct = total ? Math.min(100, (used / total) * 100) : 0;
  const tone = pct >= 90 ? "bad" : pct >= 75 ? "warn" : "";
  return (
    <div className={`membar ${limit ? "" : "nolimit"} ${tone}`} role="img" aria-label={`${Math.round(pct)}% of ${limit ? "limit" : "host memory"}`}>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

const VERDICT = {
  ready: { tone: "ok", label: "Ready to promote" },
  blocked: { tone: "bad", label: "Not ready" },
  insync: { tone: "run", label: "Nothing to promote" },
  behind: { tone: "warn", label: "Behind production" },
};

function RefLine({ label, d, now }) {
  return (
    <div className="ref-line">
      <span className="k">{label}</span>
      <span className="mono">{d ? d.ref || "no ref" : "no successful deploy"}</span>
      {d && <span className="muted">{ago(d.created_at, now)}</span>}
    </div>
  );
}

// One staging site: can it go to production, and what would change if it did.
function PromotionCard({ site, r, now }) {
  const v = VERDICT[r.verdict];
  return (
    <div className={`card promo ${r.verdict}`}>
      <div className="site-head">
        <Dot state={r.verdict === "blocked" ? "down" : r.verdict === "ready" ? "up" : r.verdict === "behind" ? "warn" : "idle"} />
        <b>{site.name}</b>
      </div>
      <div className="stack-state">
        <Pill tone={v.tone}>{v.label}</Pill>
      </div>

      {r.blockers.length > 0 ? (
        <ul className="checks">
          {r.blockers.map((b) => (
            <li key={b}>
              <span className="x" aria-hidden="true">✕</span> {b}
            </li>
          ))}
        </ul>
      ) : (
        <p className="checks-ok">
          <span aria-hidden="true">✓</span> deploy succeeded · healthy · no open incidents
        </p>
      )}

      <div className="compare">
        <RefLine label="production" d={r.compare.production} now={now} />
        <RefLine label="staging" d={r.compare.staging} now={now} />
        <p className="hint">{r.compare.text}</p>
      </div>
    </div>
  );
}

function StackCard({ s, deploy, now, onRemove }) {
  const silent = s.stale;
  const running = s.state === "running" || s.state === "degraded";
  const unlimited = running && s.mem_limit_mb == null;
  return (
    <div className={`card stack ${silent ? "silent" : s.restart_loop ? "loop" : s.state}`}>
      <div className="site-head">
        <Dot state={silent ? "warn" : s.restart_loop ? "down" : STATE_DOT[s.state]} />
        <b>{s.stack}</b>
        <span className="env">{s.site}</span>
      </div>

      <div className="stack-state">
        {silent ? <Pill tone="warn">reporter silent</Pill> : <Pill tone={TONE[s.state]}>{s.state}</Pill>}
        {!silent && s.restart_loop && <Pill tone="bad">restart loop · {s.restarts_10m} in 10 min</Pill>}
        {deploy && <Pill tone={DEPLOY_TONE[deploy.status]}>deploy {deploy.status}</Pill>}
      </div>

      <div className="stack-mem">
        <div className="metric">
          <span className="k">memory</span>
          <span className="v">
            {s.mem_mb == null ? "—" : mem(s.mem_mb)}
            {s.mem_limit_mb != null && <small> / {mem(s.mem_limit_mb)}</small>}
          </span>
        </div>
        <div className="metric">
          <span className="k">disk</span>
          <span className="v">{s.disk_mb == null ? "—" : mem(s.disk_mb)}</span>
        </div>
        <div className="metric">
          <span className="k">cpu</span>
          <span className="v">
            <Odometer value={s.cpu_pct == null ? "—" : s.cpu_pct.toFixed(1)} />
            <small>%</small>
          </span>
        </div>
      </div>
      {running && <MemBar used={s.mem_mb ?? 0} limit={s.mem_limit_mb} host={s.host_mem_mb} />}
      {unlimited && <p className="warn-note">no memory limit set: one runaway app can take the host</p>}

      <Spark points={s.mem_series} width={220} height={28} tone="var(--dim)" />

      <ul className="svc">
        {s.services.length === 0 ? (
          <li className="muted">no containers</li>
        ) : (
          s.services.map((v) => (
            <li key={v.name}>
              <Dot state={v.state === "running" ? "up" : v.state === "unhealthy" ? "down" : "idle"} />
              <span className="svc-name">
                {v.name}
                {v.note && <small className="svc-note">{v.note}</small>}
              </span>
              <span className="svc-mem">
                {v.mem_mb == null ? v.state : mem(v.mem_mb)}
                {v.restarts > 0 && <span className="c-warn"> · {plural(v.restarts, "restart", "restarts")}</span>}
              </span>
            </li>
          ))
        )}
      </ul>

      <div className="site-foot">
        <span className={silent ? "c-warn" : ""}>
          {silent ? "last report " : "reported "}
          {ago(s.reported_at, now)}
        </span>
        {silent && (
          <button className="btn" onClick={() => onRemove(s)} title="Forget this stack, if it was decommissioned">
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

function StackHowTo() {
  return (
    <div className="card howto">
      <h2>No stack reports yet</h2>
      <p>
        Nightjar's hub is on a VPS and cannot see the machine that hosts staging, so a small reporter
        on that machine pushes each stack's state in. It reports only the Compose projects you list.
      </p>
      <pre>{`# stacks.json: Compose project name -> that staging site's ingest key
{"agrovet": {"key": "<ingest_key>", "secret": "<ingest_secret>"}}

# cron, every minute
* * * * * NIGHTJAR_URL=https://nightjar.example \\
  NIGHTJAR_STACKS=/opt/nightjar/stacks.json \\
  python /opt/nightjar/stack_reporter.py`}</pre>
      <p className="hint">
        If the reporter stops, the stack is shown as <code>reporter silent</code>, never as healthy.
      </p>
    </div>
  );
}

export default function Staging({ f }) {
  const { sites, deploys, stacks, now, reloadStacks } = f;
  const [error, setError] = useState(null);

  // Only offered for a silent stack: a stack that stops reporting stays visible
  // until someone says it was decommissioned.
  const remove = async (s) => {
    if (!confirm(`Forget stack "${s.stack}"? Its history is deleted. Only do this if it was decommissioned.`)) return;
    setError(null);
    try {
      await send("DELETE", `/api/stacks?site=${encodeURIComponent(s.site)}&stack=${encodeURIComponent(s.stack)}`);
      await reloadStacks();
    } catch (err) {
      setError(err.message);
    }
  };
  const staging = sites.filter((s) => s.env === "staging");
  const stagingDeploys = deploys.filter((d) => d.env === "staging");
  const stagingStacks = stacks.filter((s) => s.env === "staging");

  const live = stagingStacks.filter((s) => !s.stale);
  const running = live.filter((s) => s.state === "running").length;
  const used = live.reduce((n, s) => n + (s.mem_mb ?? 0), 0);
  const host = Math.max(0, ...stagingStacks.map((s) => s.host_mem_mb ?? 0));
  const unlimited = live.filter((s) => s.state !== "stopped" && s.mem_limit_mb == null).length;
  const silent = stagingStacks.filter((s) => s.stale).length;
  const hostPct = host ? (used / host) * 100 : 0;

  // Host disk is a property of the machine, so any live report will do.
  const disk = live.find((s) => s.host_disk_total_mb);
  const diskFreePct = disk ? (disk.host_disk_free_mb / disk.host_disk_total_mb) * 100 : null;
  const builds = runningBuilds(deploys, now).length;
  const promotions = staging.map((site) => ({ site, r: readiness({ site, sites, deploys, stacks, now }) }));

  const latestDeploy = (site) => stagingDeploys.find((d) => d.site === site);

  return (
    <>
      <div className="page-head">
        <h1>Staging</h1>
        <span className="count">
          {plural(stagingStacks.length, "stack", "stacks")} · {plural(staging.length, "environment", "environments")}
        </span>
        {CONTROL_URL && (
          <a className="btn pri manage" href={CONTROL_URL} target="_blank" rel="noopener noreferrer">
            Manage stacks ↗
          </a>
        )}
      </div>
      {error && <p className="form-error">{error}</p>}
      {!CONTROL_URL && (
        <p className="hint control-hint">
          This view is read-only. Start, stop and logs live in Dockge or Portainer on the staging host.
          Set <code>VITE_STACK_CONTROL_URL</code> to link it here.
        </p>
      )}

      {promotions.length > 0 && (
        <>
          <div className="sec">
            <h2>Promotion</h2>
            <span className="count">can it go to production?</span>
          </div>
          <div className="site-grid">
            {promotions.map(({ site, r }) => (
              <PromotionCard key={site.id} site={site} r={r} now={now} />
            ))}
          </div>
        </>
      )}

      {stagingStacks.length === 0 ? (
        <StackHowTo />
      ) : (
        <>
          <section className="stats-strip" aria-label="Staging host">
            <div className="card stat">
              <span className="k">Stacks running</span>
              <span className="big"><Odometer value={running} /><small>/ {stagingStacks.length}</small></span>
              <span className="sub">{silent ? `${silent} reporter silent` : "all reporting"}</span>
            </div>
            <div className={`card stat ${hostPct >= 90 ? "bad" : hostPct >= 75 ? "warn" : ""}`}>
              <span className="k">Memory used</span>
              <span className="big">{used >= 1024 ? gb(used) : `${used} MiB`}</span>
              <span className="sub">{host ? `of ${gb(host)} on the host` : "host size unknown"}</span>
            </div>
            <div className={`card stat ${unlimited ? "warn" : ""}`}>
              <span className="k">Without a limit</span>
              <span className="big"><Odometer value={unlimited} /></span>
              <span className="sub">{unlimited ? "running with no memory limit" : "every stack is limited"}</span>
            </div>
            <div className={`card stat ${diskFreePct == null ? "" : diskFreePct < 10 ? "bad" : diskFreePct < 20 ? "warn" : ""}`}>
              <span className="k">Host disk free</span>
              <span className="big">{diskFreePct == null ? "—" : `${Math.round(diskFreePct)}%`}</span>
              <span className="sub">
                {disk ? `${gb(disk.host_disk_free_mb)} of ${gb(disk.host_disk_total_mb)}` : "not reported"}
              </span>
            </div>
            <div className={`card stat ${builds > 1 ? "warn" : ""}`}>
              <span className="k">Builds running</span>
              <span className="big"><Odometer value={builds} /></span>
              <span className="sub">
                {builds > 1 ? "more than one at once: watch RAM and heat" : builds === 1 ? "one at a time is the rule" : "idle"}
              </span>
            </div>
          </section>

          <div className="sec">
            <h2>Stacks</h2>
            <span className="count">reported by the staging host</span>
          </div>
          <div className="site-grid">
            {stagingStacks.map((s) => (
              <StackCard key={`${s.site}/${s.stack}`} s={s} deploy={latestDeploy(s.site)} now={now} onRemove={remove} />
            ))}
          </div>
        </>
      )}

      <div className="sec">
        <h2>Environments</h2>
        <span className="count">uptime checks</span>
      </div>
      {staging.length === 0 ? (
        <p className="empty">
          No staging environment yet. A staging environment is just a site with env <code>staging</code>{" "}
          and a <code>url</code>, so it gets the same checks as production.
        </p>
      ) : (
        <div className="site-grid">
          {staging.map((s) => (
            <SiteCard key={s.id} site={s} now={now} />
          ))}
        </div>
      )}

      <div className="sec">
        <h2>Staging deploys</h2>
        <span className="count">{stagingDeploys.length} recorded</span>
      </div>
      {stagingDeploys.length === 0 ? (
        <DeployHowTo env="staging" />
      ) : (
        <DeployList deploys={stagingDeploys} now={now} />
      )}
    </>
  );
}
