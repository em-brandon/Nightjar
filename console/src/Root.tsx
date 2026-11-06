import { useCallback, useEffect, useState } from "react";
import App from "./App.tsx";
import Login from "./Login.tsx";

// Decides whether to show the console or the login. The console only mounts once
// we are allowed in, so nothing polls the hub (and gets a wall of 401s) before then.
export default function Root() {
  const [phase, setPhase] = useState<"checking" | "login" | "ready">("checking");
  const [required, setRequired] = useState(false);
  // true when the hub signs people in with an account (and so has sessions to list), false for the shared token
  const [account, setAccount] = useState(false);
  // bumped on each sign-in, so the console remounts with fresh feeds
  const [session, setSession] = useState(0);

  const check = useCallback(async () => {
    try {
      const res = await fetch("/api/auth/status");
      const status = (await res.json()) as { required: boolean; authenticated: boolean; account?: boolean };
      setRequired(status.required);
      setAccount(Boolean(status.account));
      setPhase(!status.required || status.authenticated ? "ready" : "login");
    } catch {
      // The hub is unreachable, so we cannot tell whether a login is needed. Show
      // the console: it says plainly that it cannot reach the hub, and a 401 once
      // the hub is back brings up the login.
      setPhase("ready");
    }
  }, []);

  useEffect(() => {
    check();
  }, [check]);

  // Any request that comes back 401 (an expired or revoked session) lands here.
  useEffect(() => {
    const onUnauthorized = () => setPhase("login");
    addEventListener("nj-unauthorized", onUnauthorized);
    return () => removeEventListener("nj-unauthorized", onUnauthorized);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      setPhase("login");
    }
  }, []);

  // Render nothing while checking, so a signed-in user does not see the login flash.
  if (phase === "checking") return null;
  if (phase === "login") {
    return (
      <Login
        account={account}
        onSuccess={() => {
          setRequired(true);
          setSession((n) => n + 1);
          setPhase("ready");
        }}
      />
    );
  }
  return <App key={session} onSignOut={required ? signOut : undefined} hasAccount={account} />;
}
