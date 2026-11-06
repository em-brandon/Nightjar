import { useState, type FormEvent } from "react";
import { getPasskey, passkeyProblem, supported, type WireOptions } from "./passkey.ts";

// Two forms, chosen by the hub (/api/auth/status). With accounts on (production) it asks for a username, a
// password and an authenticator code; in local development it asks for the shared console token. Either way the
// browser never keeps a secret: the hub answers a successful login with an HttpOnly session cookie that page
// JavaScript cannot read.
export default function Login({ onSuccess, account, onSetup }: { onSuccess: () => void; account: boolean; onSetup?: () => void }) {
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(account ? { username, password, code } : { token }),
      });
      if (res.status === 429) throw new Error("Too many attempts. Wait a few minutes and try again.");
      if (!res.ok) throw new Error(account ? "That username, password or code isn't right." : "That token isn't right.");
      setToken("");
      setPassword("");
      setCode("");
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  // A passkey replaces all three fields: the browser asks for a fingerprint, face or PIN and the hub checks the signature.
  const withPasskey = async () => {
    setBusy(true);
    setError(null);
    try {
      const asked = await fetch("/api/auth/passkey/options", { method: "POST" });
      if (!asked.ok) throw new Error("Passkeys are not available right now.");
      const { challenge_id, options } = (await asked.json()) as { challenge_id: string; options: WireOptions };
      const credential = await getPasskey(options);
      const res = await fetch("/api/auth/passkey/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ challenge_id, credential }),
      });
      if (res.status === 429) throw new Error("Too many attempts. Wait a few minutes and try again.");
      if (!res.ok) throw new Error("That passkey was not accepted.");
      onSuccess();
    } catch (err) {
      setError(passkeyProblem(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="brand">
          night<span>jar</span>
        </div>
        {account ? (
          <>
            <p className="hint">Sign in to continue.</p>
            <label>
              Username
              <input
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                maxLength={60}
              />
            </label>
            <label>
              Password
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                maxLength={200}
              />
            </label>
            <label>
              Authenticator or recovery code
              <input
                inputMode="text"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                maxLength={32}
              />
            </label>
          </>
        ) : (
          <>
            <p className="hint">Enter the console token to continue.</p>
            <label>
              Token
              <input
                type="password"
                autoComplete="current-password"
                autoFocus
                value={token}
                onChange={(e) => setToken(e.target.value)}
                maxLength={200}
              />
            </label>
          </>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="btn pri" disabled={busy || (account ? !username || !password || !code : !token)}>
          Sign in
        </button>
        {account && supported() && (
          <button type="button" className="btn" disabled={busy} onClick={withPasskey}>
            Sign in with a passkey
          </button>
        )}
        {account && onSetup && (
          <button type="button" className="side-link" onClick={onSetup}>
            I have a setup token
          </button>
        )}
      </form>
    </main>
  );
}
