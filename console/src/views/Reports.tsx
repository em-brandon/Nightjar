import { useEffect, useState } from "react";
import type { AvailabilityReport } from "../api/types.ts";
import type { Feeds } from "../feeds.ts";
import { fmtTime, plural, usePoll } from "../hooks.ts";
import { canGoForward, downtimeText, duration, monthLabel, monthOf, pctText, shiftMonth, verdictLabel, verdictTone } from "../reports.ts";
import { Dot, Empty, Pill } from "../ui.tsx";

type Client = AvailabilityReport["clients"][number];
type Site = Client["sites"][number];

// How each client's sites did over a month, and exactly what a client report would say. Nothing here sends anything:
// the switch is shown so the plan is visible, and stays off until the hub can send email (decision #58). Every figure
// comes from /api/reports/availability; this file only words it.

const tone = (s: Site) => (s.uptime_pct == null ? "run" : s.met_target ? "ok" : "bad");

function Sheet({ r, client, onClose }: { r: AvailabilityReport; client: Client; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);

  const minutes = Math.round(r.probe_interval_s / 60) || 1;
  return (
    <div className="rp-overlay" role="dialog" aria-modal="true" aria-labelledby="rp-sheet-title" onClick={onClose}>
      <article className="rp-sheet" onClick={(e) => e.stopPropagation()}>
        <div className="rp-sheet-bar">
          <span className="muted">What {client.name} would receive</span>
          <span>
            <button className="btn" onClick={() => window.print()}>
              Print / Save as PDF
            </button>{" "}
            <button className="btn pri" onClick={onClose}>
              Close
            </button>
          </span>
        </div>
        <h2 id="rp-sheet-title">Website availability report</h2>
        <p className="rp-sheet-sub">
          {client.name} · {monthLabel(r.month)}
          {r.partial && ` (to ${fmtTime(r.through)})`}
        </p>
        <p className={`rp-headline ${verdictTone(client.verdict)}`}>{client.headline}</p>
        <table className="rp-table">
          <thead>
            <tr>
              <th>Site</th>
              <th>Availability</th>
              <th>Downtime</th>
              <th>Average response</th>
            </tr>
          </thead>
          <tbody>
            {client.sites.map((s) => (
              <tr key={s.site_id}>
                <td>
                  {s.name}
                  <small>{s.host}</small>
                </td>
                <td>{pctText(s.uptime_pct)}</td>
                <td>{downtimeText(s.downtime_minutes, s.checks_total)}</td>
                <td>{s.latency_avg_ms == null ? "—" : `${s.latency_avg_ms} ms`}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {client.sites.some((s) => s.incidents.length > 0) && (
          <>
            <h3>Outages</h3>
            <ul className="rp-incidents">
              {client.sites.flatMap((s) =>
                s.incidents.map((i) => (
                  <li key={`${s.site_id}-${i.started_at}`}>
                    <b>{s.name}</b> was unavailable from {fmtTime(i.started_at)}
                    {i.ongoing ? ", and still is" : ` for about ${duration(i.minutes)}`}.
                  </li>
                )),
              )}
            </ul>
          </>
        )}
        {client.sites.some((s) => s.deploys > 0) && (
          <p className="rp-foot">Updates released this month: {client.sites.reduce((n, s) => n + s.deploys, 0)}.</p>
        )}
        <p className="rp-foot">
          Availability is the share of automated checks, made about every {minutes} {minutes === 1 ? "minute" : "minutes"}, that
          succeeded. Downtime is an estimate from those checks. Target: {r.target_pct}%. Times are UTC.
        </p>
      </article>
    </div>
  );
}

function SiteRow({ s }: { s: Site }) {
  return (
    <div className="rp-site">
      <span className="rp-site-name">
        <Dot state={s.uptime_pct == null ? "warn" : s.met_target ? "up" : "down"} />
        <b>{s.name}</b>
        <small className="muted">{s.host}</small>
      </span>
      <span className={`rp-pct ${tone(s)}`}>{pctText(s.uptime_pct)}</span>
      <span className="mono muted">{downtimeText(s.downtime_minutes, s.checks_total)}</span>
      <span className="mono muted">{s.latency_avg_ms == null ? "—" : `${s.latency_avg_ms} ms`}</span>
      <span className="mono muted">
        {s.incidents.length === 0 ? "no outages" : plural(s.incidents.length, "outage", "outages")}
        {!s.incident_detail && " (list not kept)"}
      </span>
      {s.coverage_pct != null && s.coverage_pct < 90 && (
        <small className="rp-warn">Only {s.coverage_pct}% of expected checks ran: monitoring has a gap.</small>
      )}
    </div>
  );
}

function ClientCard({ client, onPreview }: { client: Client; onPreview: () => void }) {
  return (
    <section className="card rp-client">
      <header className="rp-client-head">
        <h2>{client.name}</h2>
        <Pill tone={verdictTone(client.verdict)}>{verdictLabel(client.verdict)}</Pill>
        <span className="rp-big">{pctText(client.uptime_pct)}</span>
      </header>
      <p className="muted">{client.headline}</p>
      <div className="rp-sites">
        {client.sites.map((s) => (
          <SiteRow key={s.site_id} s={s} />
        ))}
      </div>
      {client.not_monitored.length > 0 && (
        <p className="hint">Not monitored (no address set, so no figure is claimed): {client.not_monitored.join(", ")}.</p>
      )}
      <footer className="rp-client-foot">
        <label className="rp-switch" title="Automatic sending is not built yet">
          <input type="checkbox" role="switch" checked={false} disabled readOnly aria-describedby={`rp-why-${client.client_id}`} />
          <span>Email this report each month</span>
        </label>
        <small id={`rp-why-${client.client_id}`} className="muted">
          Off, and cannot be turned on yet: Nightjar has no email sender.{" "}
          {client.contact_email ? `It would go to ${client.contact_email}.` : "No contact address is set for this client."}
        </small>
        <button className="btn" onClick={onPreview}>
          Preview client report
        </button>
      </footer>
    </section>
  );
}

export default function Reports({ f }: { f: Feeds }) {
  const [month, setMonth] = useState(() => monthOf(f.now));
  const [open, setOpen] = useState<number | null>(null);
  // a report changes slowly: every two minutes is plenty
  const rep = usePoll<AvailabilityReport>(`/api/reports/availability?month=${month}`, 120_000);
  const r = rep.data && rep.data.month === month ? rep.data : null;

  const clients = r?.clients ?? [];
  const incidents = clients.reduce((n, c) => n + c.sites.reduce((m, s) => m + s.incidents.length, 0), 0);
  const below = clients.filter((c) => c.verdict === "below").length;
  const sheetClient = clients.find((c) => c.client_id === open);

  return (
    <>
      <div className="page-head">
        <h1>Reports</h1>
        <span className="count">{r ? `${monthLabel(r.month)}${r.partial ? " so far" : ""}` : monthLabel(month)}</span>
        <span className="rp-nav">
          <button className="btn" onClick={() => setMonth(shiftMonth(month, -1))} aria-label="Previous month">
            ‹
          </button>
          <button className="btn" onClick={() => setMonth(monthOf(f.now))} disabled={month === monthOf(f.now)}>
            This month
          </button>
          <button className="btn" onClick={() => setMonth(shiftMonth(month, 1))} disabled={!canGoForward(month, f.now)} aria-label="Next month">
            ›
          </button>
        </span>
      </div>

      {rep.error && !r && (
        <p className="form-error" role="alert">
          Could not load the report: {rep.error}. {rep.error.includes("422") ? "That month is outside the range reports cover." : ""}
        </p>
      )}
      {!r && !rep.error && <Empty>Loading the report…</Empty>}

      {r && (
        <>
          <div className="rp-strip">
            <div>
              <small>Availability, all client sites</small>
              <b className="rp-big">{pctText(r.overall_uptime_pct)}</b>
            </div>
            <div>
              <small>Target</small>
              <b>{r.target_pct}%</b>
            </div>
            <div>
              <small>Clients below target</small>
              <b className={below ? "c-p1" : ""}>{below}</b>
            </div>
            <div>
              <small>Outages</small>
              <b>{incidents}</b>
            </div>
          </div>
          {r.partial && <p className="hint">This month is not over. Figures are to {fmtTime(r.through)} UTC.</p>}
          {clients.length === 0 ? (
            <Empty>No production site is set up for a client yet, so there is nothing to report.</Empty>
          ) : (
            clients.map((c) => <ClientCard key={c.client_id} client={c} onPreview={() => setOpen(c.client_id)} />)
          )}
          <p className="hint">
            Figures are the share of uptime checks that succeeded. A site with no checks shows a dash, never 100%, and a gap in
            monitoring is called out. Staging is not included. Months run on UTC days.
          </p>
        </>
      )}
      {r && sheetClient && <Sheet r={r} client={sheetClient} onClose={() => setOpen(null)} />}
    </>
  );
}
