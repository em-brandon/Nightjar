import { useState } from "react";
import type { Alert, AlertsStatus, TestPush } from "../api/types.ts";
import type { Feeds } from "../feeds.ts";
import { alertState, senderHealth } from "../alerts.ts";
import { ago, fmtTime, plural, send, usePoll } from "../hooks.ts";
import { Dot, Empty, Pill, Sev } from "../ui.tsx";

// The alert path, end to end: can a P1 reach the phone right now, what has been sent, what has not, and a
// button that proves it. Nothing here changes what alerts or how loudly (that is the policy, hub/policy.py);
// it only shows what the outbox did, and lets a failed alert be tried again.

const why = (err: unknown) => (err instanceof Error ? err.message : String(err));

function Row({ ok, label, children, hint }: { ok: "ok" | "warn" | "bad"; label: string; children: string; hint?: string }) {
  return (
    <li className="al-check">
      <Dot state={ok === "ok" ? "up" : ok === "warn" ? "warn" : "down"} />
      <span className="al-label">{label}</span>
      <span className={ok === "ok" ? "muted" : ok === "warn" ? "c-warn" : "c-p1"}>{children}</span>
      {hint && <small className="al-hint">{hint}</small>}
    </li>
  );
}

function StatusPanel({ status, now, onTested }: { status: AlertsStatus; now: number; onTested: () => void }) {
  const sender = senderHealth(status, now);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const test = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await send<TestPush>("POST", "/api/notifications/test-push");
      setResult(
        r?.sent
          ? { ok: true, text: "Sent. Your phone should show it within a few seconds. If it does not, the key or app token is wrong for this account." }
          : { ok: false, text: `Not sent: ${r?.error ?? "the hub gave no reason"}${r?.retryable ? " (this may clear: try again)" : ""}` },
      );
    } catch (err) {
      setResult({ ok: false, text: `Could not ask the hub: ${why(err)}` });
    } finally {
      setBusy(false);
      onTested();
    }
  };

  return (
    <section className="card al-panel" aria-label="Alert path status">
      <h2>Would a P1 reach your phone?</h2>
      <ul className="al-checks">
        <Row
          ok={status.pushover_configured ? "ok" : "bad"}
          label="Pushover"
          hint={status.pushover_configured ? undefined : "Set PUSHOVER_TOKEN and PUSHOVER_USER in the hub's environment. Until then every alert fails here and nothing reaches the phone."}
        >
          {status.pushover_configured ? "configured" : "not configured"}
        </Row>
        <Row ok={sender.tone} label="Sender">
          {sender.text}
        </Row>
        <Row
          ok={status.callback_configured ? "ok" : "warn"}
          label="Acknowledge on the phone"
          hint={status.callback_configured ? undefined : "The hub checks Pushover for acknowledgements every 30 seconds. For them to arrive instantly it needs HUB_PUBLIC_URL and PUSHOVER_CALLBACK_TOKEN, and a public address."}
        >
          {status.callback_configured ? "instant (callback set)" : "found by checking every 30s"}
        </Row>
        <Row
          ok={status.deadman_configured ? "ok" : "warn"}
          label="Dead-man's switch"
          hint={status.deadman_configured ? undefined : "Set DEADMAN_URL to a healthchecks.io ping URL. Without it nobody is told if the hub itself dies."}
        >
          {status.deadman_configured ? "pinged while the sender is alive" : "not set"}
        </Row>
      </ul>

      <div className="al-counts" aria-label="Counts">
        <span><b>{status.pending}</b> waiting</span>
        <span className={status.retrying > 0 ? "c-warn" : ""}><b>{status.retrying}</b> retrying</span>
        <span className={status.failed > 0 ? "c-p1" : ""}><b>{status.failed}</b> failed</span>
        <span><b>{status.sent_24h}</b> sent in 24h</span>
      </div>

      <div className="al-actions">
        <button className="btn pri" disabled={busy || !status.pushover_configured} onClick={test}>
          Send a test push
        </button>
        {!status.pushover_configured && <span className="muted">Needs Pushover to be configured first.</span>}
        {result && (
          <span className={result.ok ? "muted" : "form-error"} role="status">
            {result.text}
          </span>
        )}
      </div>
    </section>
  );
}

function AlertRow({ a, now, onRetried }: { a: Alert; now: number; onRetried: () => void }) {
  const state = alertState(a);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      await send("POST", `/api/notifications/${a.id}/retry`);
      onRetried();
    } catch (err) {
      setError(why(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="al-row">
      <Sev level={a.severity} />
      <span className="al-main">
        <span className="who">
          {a.site}
          <Pill tone={state.tone}>{state.label}</Pill>
        </span>
        <span className="detail">{a.title}</span>
        {a.last_error && a.status !== "sent" && <span className="al-err">{a.last_error}</span>}
        {error && <span className="form-error">{error}</span>}
      </span>
      <span className="al-when" title={fmtTime(a.created_at)}>
        {ago(a.created_at, now)}
      </span>
      {a.status === "failed" && (
        <button className="btn" disabled={busy} onClick={retry}>
          Retry
        </button>
      )}
    </div>
  );
}

export default function Alerts({ f }: { f: Feeds }) {
  // Polled here, faster than the shell's 30 seconds: this is the screen you watch while proving the path.
  const status = usePoll<AlertsStatus>("/api/notifications/status", 5_000);
  const [show, setShow] = useState<"attention" | "all">("attention");
  const list = usePoll<Alert[]>(`/api/notifications?status=${show}&limit=100`, 5_000);
  const { now } = f;

  const reload = () => Promise.all([status.reload(), list.reload(), f.reloadAttention()]);
  const alerts = list.data ?? [];

  return (
    <>
      <div className="page-head">
        <h1>Alerts</h1>
        <span className="count">{f.attention.length > 0 ? `${plural(f.attention.length, "needs", "need")} attention` : "all delivered"}</span>
      </div>

      {status.error && (
        <p className="form-error" role="alert">
          Can't reach the hub{status.data ? ", so this may be out of date" : ""}.
        </p>
      )}

      {status.data ? <StatusPanel status={status.data} now={now} onTested={reload} /> : <Empty>{status.error ? "No status yet." : "Loading…"}</Empty>}

      <div className="sec">
        <h2>History</h2>
        <div className="chips" role="group" aria-label="Filter">
          {(
            [
              ["attention", "Needs attention"],
              ["all", "All"],
            ] as const
          ).map(([k, label]) => (
            <button key={k} className={`chip ${show === k ? "on" : ""}`} onClick={() => setShow(k)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {list.data === null ? (
        <Empty>{list.error ? "No list yet." : "Loading…"}</Empty>
      ) : alerts.length === 0 ? (
        <Empty>{show === "attention" ? "Nothing needs attention. Every alert that was due has been delivered." : "No alerts yet. A P1 or P2 will appear here the moment it is queued."}</Empty>
      ) : (
        <div className="al-list">
          {alerts.map((a) => (
            <AlertRow key={a.id} a={a} now={now} onRetried={reload} />
          ))}
        </div>
      )}
    </>
  );
}
