import { useState, type FormEvent } from "react";

// Finishing a new account. An owner gave you a one-time setup token. Step 1: paste it and choose a password. Step 2:
// add the secret to an authenticator app, keep the recovery codes, and prove it works with a code. Until step 2 is done
// the account cannot sign in. Nothing here is kept in the browser.

interface Started {
  username: string;
  totp_secret: string;
  totp_uri: string;
  recovery_codes: string[];
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    let why = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (typeof j.detail === "string") why = j.detail;
    } catch {
      /* keep the status */
    }
    throw new Error(why);
  }
  return (await res.json()) as T;
}

export default function Setup({ onDone, onBack }: { onDone: () => void; onBack: () => void }) {
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [code, setCode] = useState("");
  const [started, setStarted] = useState<Started | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const start = (e: FormEvent) => {
    e.preventDefault();
    if (password !== again) return setError("The two passwords are not the same.");
    return run(async () => setStarted(await post<Started>("/api/auth/setup/start", { token: token.trim(), password })));
  };

  const confirm = (e: FormEvent) => {
    e.preventDefault();
    return run(async () => {
      await post("/api/auth/setup/confirm", { token: token.trim(), code: code.trim() });
      setToken("");
      setPassword("");
      setAgain("");
      onDone();
    });
  };

  return (
    <main className="login">
      <form className="card login-card" onSubmit={started ? confirm : start}>
        <div className="brand">
          night<span>jar</span>
        </div>
        {!started ? (
          <>
            <p className="hint">Set up your account. Paste the setup token an owner gave you and choose a password (at least 12 characters).</p>
            <label>
              Setup token
              <input autoFocus value={token} onChange={(e) => setToken(e.target.value)} maxLength={200} autoComplete="off" />
            </label>
            <label>
              Password
              <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} maxLength={200} />
            </label>
            <label>
              Password again
              <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} maxLength={200} />
            </label>
          </>
        ) : (
          <>
            <p className="hint">
              Welcome, {started.username}. Add this to an authenticator app (use the link on a phone, or type the secret):
            </p>
            <code className="access-secret-code">{started.totp_secret}</code>
            <a className="side-link" href={started.totp_uri}>Open in an authenticator app</a>
            <p className="hint">Keep these recovery codes somewhere safe. Each works once if you lose the app. They are not shown again.</p>
            <code className="access-secret-code">{started.recovery_codes.join("   ")}</code>
            <label>
              Code from the app, to prove it works
              <input inputMode="numeric" autoComplete="one-time-code" autoFocus value={code} onChange={(e) => setCode(e.target.value)} maxLength={32} />
            </label>
          </>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="btn pri" disabled={busy || (started ? !code : !token || !password || !again)}>
          {started ? "Finish setup" : "Continue"}
        </button>
        <button type="button" className="btn" onClick={onBack}>Back to sign in</button>
      </form>
    </main>
  );
}
