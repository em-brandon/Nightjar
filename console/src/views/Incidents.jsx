import { useCallback, useEffect, useRef, useState } from "react";
import { ago, fmtTime, send } from "../hooks.ts";
import { Empty, Odometer, Pill, Sev } from "../ui.tsx";

const STATUS_TONE = { open: "bad", resolved: "ok", muted: "run", snoozed: "warn" };

// Which actions make sense in which state. The hub enforces the same rules;
// this only stops the console offering buttons that would be refused.
function actionsFor(issue) {
  if (issue.status === "open") {
    return [
      !issue.acknowledged && { action: "ack", label: "Acknowledge", primary: true },
      { action: "resolve", label: "Resolve", primary: issue.acknowledged },
      { action: "mute", label: "Mute" },
      { action: "snooze", label: "Snooze 24h", hours: 24 },
    ].filter(Boolean);
  }
  return [{ action: "reopen", label: "Reopen", primary: true }];
}

function IssueRow({ issue, now, onOpen }) {
  const quiet = issue.status !== "open";
  return (
    <button className={`issue ${quiet ? "quiet" : ""}`} onClick={() => onOpen(issue.id)}>
      <Sev level={issue.severity} />
      <span className="issue-main">
        <span className="who">
          {issue.site}
          <span className="env">{issue.env}</span>
          {quiet && <Pill tone={STATUS_TONE[issue.status]}>{issue.status}</Pill>}
          {issue.status === "open" && !issue.acknowledged && <Pill tone="bad">unacked</Pill>}
        </span>
        <span className="detail">{issue.title}</span>
      </span>
      <span className="right">
        <span className="n">×{issue.event_count.toLocaleString()}</span>
        <span className="ago">{ago(issue.last_seen, now)}</span>
      </span>
    </button>
  );
}

function Drawer({ issue, now, onClose, onChanged }) {
  const closeRef = useRef(null);
  const [log, setLog] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const loadLog = useCallback(async () => {
    try {
      const res = await fetch(`/api/issues/${issue.id}/log`);
      if (res.ok) setLog(await res.json());
    } catch {
      /* the log is supplementary; the actions still work without it */
    }
  }, [issue.id]);

  useEffect(() => {
    loadLog();
  }, [loadLog, issue.status, issue.acknowledged]);

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

  const run = async (a) => {
    setBusy(true);
    setError(null);
    try {
      await send("POST", `/api/issues/${issue.id}/actions`, {
        action: a.action,
        ...(a.hours ? { hours: a.hours } : {}),
      });
      await onChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const stats = [
    ["events", issue.event_count.toLocaleString()],
    ["first seen", ago(issue.first_seen, now)],
    ["last seen", ago(issue.last_seen, now)],
  ];

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Incident detail">
        <div className="hd">
          <Sev level={issue.severity} />
          <div>
            <h2>{issue.title}</h2>
            <div className="sub">
              {issue.site} · {issue.env} · <span className={`st-${issue.status}`}>{issue.status}</span>
              {issue.snoozed_until && ` until ${fmtTime(issue.snoozed_until)}`}
            </div>
          </div>
          <button ref={closeRef} className="close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>

        <div className="acts">
          {actionsFor(issue).map((a) => (
            <button
              key={a.action}
              className={`btn ${a.primary ? "pri" : ""}`}
              disabled={busy}
              onClick={() => run(a)}
            >
              {a.label}
            </button>
          ))}
        </div>
        {error && <p className="form-error">{error}</p>}
        {issue.status === "muted" && (
          <p className="hint">
            Muted: this keeps counting but will never alert again, even on regression, until you
            reopen it.
          </p>
        )}

        <div className="stats">
          {stats.map(([k, v]) => (
            <div className="stat-cell" key={k}>
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
        </dl>

        <div className="sec">
          <h2>Incident log</h2>
          <span className="count">what happened, and who did what</span>
        </div>
        {log === null ? (
          <Empty>Loading…</Empty>
        ) : (
          <div className="log">
            {log.map((e, i) => (
              <div key={i} className={`ev ${e.kind}`}>
                <div className="t">{fmtTime(e.at)}</div>
                <div className="m">{e.text}</div>
              </div>
            ))}
          </div>
        )}
      </aside>
    </>
  );
}

export default function Incidents({ f }) {
  const { issues, now, reloadIssues } = f;
  const [show, setShow] = useState("open");
  const [openId, setOpenId] = useState(null);
  const close = useCallback(() => setOpenId(null), []);

  const open = issues.filter((i) => i.status === "open");
  const list = show === "open" ? open : issues;
  const selected = issues.find((i) => i.id === openId);

  return (
    <>
      <div className="page-head">
        <h1>Incidents</h1>
        <span className="count">
          <Odometer value={open.length} /> open
        </span>
        <div className="chips" role="group" aria-label="Filter">
          {[
            ["open", "Open"],
            ["all", "All"],
          ].map(([k, label]) => (
            <button key={k} className={`chip ${show === k ? "on" : ""}`} onClick={() => setShow(k)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {list.length === 0 ? (
        <Empty>{show === "open" ? "Nothing open. Enjoy it." : "No incidents recorded yet."}</Empty>
      ) : (
        <div className="issues">
          {list.map((i) => (
            <IssueRow key={i.id} issue={i} now={now} onOpen={setOpenId} />
          ))}
        </div>
      )}

      {selected && <Drawer issue={selected} now={now} onClose={close} onChanged={reloadIssues} />}
    </>
  );
}
