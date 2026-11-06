import { useEffect, useState, type FormEvent } from "react";
import type { DeviceToken, User } from "./api/types.ts";
import { ago, send, useNow, usePoll } from "./hooks.ts";

// Who and what can get into the console. Owner only (the hub refuses anyone else). Two lists:
//   people: an account with a role, brought in with a one-time setup token you hand over yourself (nothing is emailed)
//   devices: a token for a machine (the wall display, a CI job), with its own name, scope and expiry, revocable alone
// A token is shown once, here, and the hub keeps only a hash: if it is lost, make another.

const why = (err: unknown) => (err instanceof Error ? err.message : String(err));
const ROLES = ["viewer", "maintainer", "owner"] as const;

interface SetupIssued {
  user: User;
  setup_token: string;
  expires_at: string;
}
interface DeviceIssued {
  device: DeviceToken;
  token: string;
}

export default function Access({ onClose, me }: { onClose: () => void; me: string | null }) {
  const users = usePoll<User[]>("/api/users", 30_000);
  const devices = usePoll<DeviceToken[]>("/api/devices", 30_000);
  const now = useNow();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // the one place a secret is on screen; cleared by the person
  const [secret, setSecret] = useState<{ label: string; value: string } | null>(null);
  const [newUser, setNewUser] = useState("");
  const [newRole, setNewRole] = useState<(typeof ROLES)[number]>("viewer");
  const [newDevice, setNewDevice] = useState("");
  const [scope, setScope] = useState<"read" | "write">("read");
  const [days, setDays] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);

  const act = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
      await Promise.all([users.reload(), devices.reload()]);
    } catch (err) {
      setError(why(err));
    } finally {
      setBusy(false);
    }
  };

  const addUser = (e: FormEvent) => {
    e.preventDefault();
    return act(async () => {
      const made = await send<SetupIssued>("POST", "/api/users", { username: newUser.trim().toLowerCase(), role: newRole });
      if (made) setSecret({ label: `Setup token for ${made.user.username} (valid 48 hours). Give it to them; they paste it on the sign-in page.`, value: made.setup_token });
      setNewUser("");
    });
  };

  const reset = (u: User) =>
    act(async () => {
      const made = await send<SetupIssued>("POST", `/api/users/${u.id}/reset`);
      if (made) setSecret({ label: `New setup token for ${u.username}. Their password, authenticator and sessions were cleared.`, value: made.setup_token });
    });

  const patch = (u: User, body: { role?: string; disabled?: boolean }) => act(async () => void (await send("PATCH", `/api/users/${u.id}`, body)));

  const addDevice = (e: FormEvent) => {
    e.preventDefault();
    return act(async () => {
      const made = await send<DeviceIssued>("POST", "/api/devices", {
        name: newDevice.trim(),
        scope,
        expires_days: days ? Number(days) : null,
      });
      if (made) setSecret({ label: `Token for ${made.device.name}. Put it in the device's settings now: it is not shown again.`, value: made.token });
      setNewDevice("");
      setDays("");
    });
  };

  const revoke = (d: DeviceToken) => act(async () => void (await send("DELETE", `/api/devices/${d.id}`)));

  const people = users.data ?? [];
  const machines = devices.data ?? [];

  return (
    <div className="sessions-overlay" role="dialog" aria-modal="true" aria-labelledby="access-title" onClick={onClose}>
      <div className="card sessions-card access-card" onClick={(e) => e.stopPropagation()}>
        <h2 id="access-title">People and devices</h2>

        {secret && (
          <div className="access-secret" role="status">
            <p>{secret.label}</p>
            <code>{secret.value}</code>
            <div className="sessions-actions">
              <button className="btn" onClick={() => navigator.clipboard?.writeText(secret.value)}>Copy</button>
              <button className="btn pri" onClick={() => setSecret(null)}>I have saved it</button>
            </div>
          </div>
        )}

        <h3>People</h3>
        {users.error && <p className="form-error" role="alert">Could not load people: {users.error}</p>}
        <ul className="sessions-list">
          {people.map((u) => {
            const self = u.username === me;
            return (
              <li key={u.id}>
                <div>
                  <strong>{u.username}</strong> {self && <span className="muted">(you)</span>}
                  <small className="muted">
                    {u.disabled ? "disabled" : u.pending_setup ? "waiting for them to set up" : "active"} · {u.role}
                  </small>
                </div>
                <div className="access-actions">
                  <select aria-label={`Role of ${u.username}`} value={u.role} disabled={busy || self} onChange={(e) => patch(u, { role: e.target.value })}>
                    {ROLES.map((r) => (
                      <option key={r}>{r}</option>
                    ))}
                  </select>
                  <button className="btn" disabled={busy || self} onClick={() => patch(u, { disabled: !u.disabled })}>
                    {u.disabled ? "Enable" : "Disable"}
                  </button>
                  <button className="btn" disabled={busy || self} onClick={() => reset(u)}>Reset</button>
                </div>
              </li>
            );
          })}
        </ul>
        <form className="passkey-add" onSubmit={addUser}>
          <label>
            New person's username
            <input value={newUser} onChange={(e) => setNewUser(e.target.value)} maxLength={40} autoComplete="off" />
          </label>
          <label>
            Role
            <select value={newRole} onChange={(e) => setNewRole(e.target.value as (typeof ROLES)[number])}>
              {ROLES.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </label>
          <small className="muted">Viewers read. Maintainers also act on incidents and edit sites. Owners also manage secrets, people and devices.</small>
          <button className="btn pri" disabled={busy || newUser.trim().length < 3}>Add person</button>
        </form>

        <h3>Devices</h3>
        {devices.error && <p className="form-error" role="alert">Could not load devices: {devices.error}</p>}
        <ul className="sessions-list">
          {machines.map((d) => (
            <li key={d.id}>
              <div>
                <strong>{d.name}</strong> <span className="muted">{d.prefix}…</span>
                <small className="muted">
                  {d.revoked_at ? "revoked" : d.scope === "write" ? "can act" : "read only"} ·{" "}
                  {d.last_used_at ? `used ${ago(d.last_used_at, now)} from ${d.last_ip || "unknown"}` : "never used"}
                  {d.expires_at && !d.revoked_at ? ` · expires ${ago(d.expires_at, now)}` : ""}
                  {d.open_sessions > 0 ? ` · ${d.open_sessions} open` : ""}
                </small>
              </div>
              {!d.revoked_at && (
                <button className="btn" disabled={busy} onClick={() => revoke(d)}>Revoke</button>
              )}
            </li>
          ))}
          {devices.data && machines.length === 0 && <li className="muted">No devices yet.</li>}
        </ul>
        <form className="passkey-add" onSubmit={addDevice}>
          <label>
            Device name
            <input value={newDevice} onChange={(e) => setNewDevice(e.target.value)} maxLength={60} placeholder="Wall display, CI deploys" autoComplete="off" />
          </label>
          <label>
            What it may do
            <select value={scope} onChange={(e) => setScope(e.target.value as "read" | "write")}>
              <option value="read">Read only (a wall display)</option>
              <option value="write">Read and act (a script)</option>
            </select>
          </label>
          <label>
            Expires after (days, blank for never)
            <input inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} maxLength={4} />
          </label>
          <button className="btn pri" disabled={busy || !newDevice.trim()}>Create device token</button>
        </form>

        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="sessions-actions">
          <span />
          <button className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
