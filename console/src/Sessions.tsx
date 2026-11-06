import { useEffect, useState } from "react";
import type { ConsoleSession } from "./api/types.ts";
import { ago, fmtTime, plural, send, useNow, usePoll } from "./hooks.ts";
import { deviceName } from "./sessions.ts";

// Where you are signed in. Only offered when the hub uses accounts (a shared token has no sessions to list).
// Ending one takes effect on the server at once, so a lost laptop's cookie stops working immediately.

const why = (err: unknown) => (err instanceof Error ? err.message : String(err));

export default function Sessions({ onClose }: { onClose: () => void }) {
  const list = usePoll<ConsoleSession[]>("/api/auth/sessions", 15_000);
  const now = useNow();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);

  const act = async (method: string, url: string) => {
    setBusy(true);
    setError(null);
    try {
      await send(method, url);
      await list.reload();
    } catch (err) {
      setError(why(err));
    } finally {
      setBusy(false);
    }
  };

  const sessions = list.data ?? [];
  const others = sessions.filter((s) => !s.current).length;

  return (
    <div className="sessions-overlay" role="dialog" aria-modal="true" aria-labelledby="sessions-title" onClick={onClose}>
      <div className="card sessions-card" onClick={(e) => e.stopPropagation()}>
        <h2 id="sessions-title">Signed-in devices</h2>
        <p className="muted">Each browser that has signed in to this console. Ending one signs that browser out immediately.</p>
        {list.error && <p className="form-error" role="alert">Could not load the list: {list.error}</p>}
        <ul className="sessions-list">
          {sessions.map((s) => (
            <li key={s.id}>
              <div>
                <strong>{deviceName(s.user_agent)}</strong> {s.current && <span className="muted">(this device)</span>}
                <small className="muted">
                  {s.ip || "unknown address"} · signed in {fmtTime(s.created_at)} · last used {ago(s.last_seen_at, now)}
                </small>
              </div>
              {!s.current && (
                <button className="btn" disabled={busy} onClick={() => act("DELETE", `/api/auth/sessions/${s.id}`)}>
                  End
                </button>
              )}
            </li>
          ))}
          {list.data && sessions.length === 0 && <li className="muted">No other sessions.</li>}
        </ul>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="sessions-actions">
          <button className="btn" disabled={busy || others === 0} onClick={() => act("POST", "/api/auth/sessions/revoke-others")}>
            {others === 0 ? "No other devices" : `Sign out ${plural(others, "other device", "other devices")}`}
          </button>
          <button className="btn pri" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
