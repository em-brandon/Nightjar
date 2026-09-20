import { useState } from "react";
import { ago, send } from "../hooks.js";
import { Empty, Odometer, Pill } from "../ui.jsx";

const KINDS = [
  ["change", "Change"],
  ["report", "Report"],
  ["access", "Access"],
];

function NewRequest({ sites, onCreated }) {
  const [form, setForm] = useState({ kind: "change", subject: "", sender: "", site: "", due: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await send("POST", "/api/tickets", {
        kind: form.kind,
        subject: form.subject.trim(),
        sender: form.sender.trim(),
        site: form.site || null,
        due_at: form.due ? new Date(form.due).toISOString() : null,
      });
      setForm({ kind: form.kind, subject: "", sender: "", site: "", due: "" });
      await onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Log a request</h2>
      <p className="hint">
        Until email intake exists (chunk 4), requests are logged here by hand.
      </p>
      <div className="form-grid">
        <label>
          Type
          <select value={form.kind} onChange={set("kind")}>
            {KINDS.map(([k, label]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="wide">
          Subject
          <input value={form.subject} onChange={set("subject")} required maxLength={300} placeholder="What did they ask for?" />
        </label>
        <label>
          From
          <input value={form.sender} onChange={set("sender")} maxLength={255} placeholder="name@client.com" />
        </label>
        <label>
          Site
          <select value={form.site} onChange={set("site")}>
            <option value="">—</option>
            {sites.map((s) => (
              <option key={s.id} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Due
          <input type="date" value={form.due} onChange={set("due")} />
        </label>
      </div>
      {error && <p className="form-error">{error}</p>}
      <button className="btn pri" disabled={busy || !form.subject.trim()}>
        Add request
      </button>
    </form>
  );
}

function Row({ t, now, onToggle }) {
  return (
    <div className={`req ${t.status}`}>
      <span className={`kind ${t.kind}`}>{t.kind}</span>
      <span className="req-main">
        <span className="subj">{t.subject}</span>
        <small>
          {t.site ?? "no site"}
          {t.sender && ` · ${t.sender}`}
        </small>
      </span>
      <span className={`age ${t.overdue ? "over" : ""}`}>
        {t.status === "closed" ? `closed ${ago(t.closed_at, now)}` : ago(t.created_at, now)}
        {t.overdue && " · overdue"}
      </span>
      <button className="btn" onClick={() => onToggle(t)}>
        {t.status === "open" ? "Close" : "Reopen"}
      </button>
    </div>
  );
}

export default function Requests({ f }) {
  const { tickets, sites, now, reloadTickets } = f;
  const [error, setError] = useState(null);
  const open = tickets.filter((t) => t.status === "open");
  const closed = tickets.filter((t) => t.status === "closed").slice(0, 10);
  const overdue = open.filter((t) => t.overdue).length;

  const toggle = async (t) => {
    setError(null);
    try {
      await send("PATCH", `/api/tickets/${t.id}`, { status: t.status === "open" ? "closed" : "open" });
      await reloadTickets();
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <>
      <div className="page-head">
        <h1>Client requests</h1>
        <span className="count">
          <Odometer value={open.length} /> open
        </span>
        {overdue > 0 && <Pill tone="bad">{overdue} overdue</Pill>}
      </div>
      {error && <p className="form-error">{error}</p>}

      {open.length === 0 ? (
        <Empty>Inbox clear. No open requests.</Empty>
      ) : (
        <div className="reqs">
          {open.map((t) => (
            <Row key={t.id} t={t} now={now} onToggle={toggle} />
          ))}
        </div>
      )}

      <NewRequest sites={sites} onCreated={reloadTickets} />

      {closed.length > 0 && (
        <>
          <div className="sec">
            <h2>Recently closed</h2>
            <span className="count">{closed.length}</span>
          </div>
          <div className="reqs">
            {closed.map((t) => (
              <Row key={t.id} t={t} now={now} onToggle={toggle} />
            ))}
          </div>
        </>
      )}
    </>
  );
}
