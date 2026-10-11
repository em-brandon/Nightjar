import { useState, type FormEvent } from "react";

// The console token is a shared secret, not a per-user password, and there is one
// operator. The browser never keeps it: the hub answers a successful login with an
// HttpOnly session cookie that page JavaScript cannot read.
export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [token, setToken] = useState("");
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
        body: JSON.stringify({ token }),
      });
      if (res.status === 429) throw new Error("Too many attempts. Wait a minute and try again.");
      if (!res.ok) throw new Error("That token isn't right.");
      setToken("");
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
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
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="btn pri" disabled={busy || !token}>
          Sign in
        </button>
      </form>
    </main>
  );
}
