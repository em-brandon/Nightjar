import { useEffect, useState, type FormEvent } from "react";
import type { Passkey } from "./api/types.ts";
import { ago, send, useNow, usePoll } from "./hooks.ts";
import { createPasskey, passkeyProblem, supported, type WireOptions } from "./passkey.ts";

// Your passkeys: a phone, laptop or security key that signs you in without a password and a code. Adding one asks for
// the password again, so a stolen browser session cannot quietly add a way back in. The password and authenticator
// code keep working, so losing a phone never locks you out.

interface Challenge {
  challenge_id: string;
  options: WireOptions;
}

export default function Passkeys({ onClose }: { onClose: () => void }) {
  const list = usePoll<Passkey[]>("/api/passkeys", 30_000);
  const now = useNow();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const challenge = await send<Challenge>("POST", "/api/passkeys/register/options");
      if (!challenge) throw new Error("The hub sent no passkey options.");
      const credential = await createPasskey(challenge.options);
      await send("POST", "/api/passkeys/register", { challenge_id: challenge.challenge_id, name: name.trim(), password, credential });
      setName("");
      setPassword("");
      await list.reload();
    } catch (err) {
      setError(passkeyProblem(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    setBusy(true);
    setError(null);
    try {
      await send("DELETE", `/api/passkeys/${id}`);
      await list.reload();
    } catch (err) {
      setError(passkeyProblem(err));
    } finally {
      setBusy(false);
    }
  };

  const passkeys = list.data ?? [];

  return (
    <div className="sessions-overlay" role="dialog" aria-modal="true" aria-labelledby="passkeys-title" onClick={onClose}>
      <div className="card sessions-card" onClick={(e) => e.stopPropagation()}>
        <h2 id="passkeys-title">Passkeys</h2>
        <p className="muted">
          Sign in with your fingerprint, face or device PIN instead of a password and a code. Your password keeps working
          as a way back in.
        </p>
        {list.error && <p className="form-error" role="alert">Could not load your passkeys: {list.error}</p>}
        <ul className="sessions-list">
          {passkeys.map((p) => (
            <li key={p.id}>
              <div>
                <strong>{p.name}</strong>
                <small className="muted">
                  added {ago(p.created_at, now)} · {p.last_used_at ? `last used ${ago(p.last_used_at, now)}` : "not used yet"}
                </small>
              </div>
              <button className="btn" disabled={busy} onClick={() => remove(p.id)} aria-label={`Remove ${p.name}`}>
                Remove
              </button>
            </li>
          ))}
          {list.data && passkeys.length === 0 && <li className="muted">No passkeys yet.</li>}
        </ul>
        {supported() ? (
          <form className="passkey-add" onSubmit={add}>
            <label>
              Name this device
              <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} placeholder="Phone, work laptop" />
            </label>
            <label>
              Your password, to confirm it is you
              <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} maxLength={200} />
            </label>
            <button className="btn pri" disabled={busy || !name.trim() || !password}>
              Add a passkey
            </button>
          </form>
        ) : (
          <p className="muted">This browser cannot use passkeys here. They need https, or localhost.</p>
        )}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="sessions-actions">
          <span />
          <button className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
