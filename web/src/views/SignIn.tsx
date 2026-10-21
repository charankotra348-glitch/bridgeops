import { useState } from "react";
import { api, ApiError } from "../api";

export function SignInView({ onSignedIn }: { onSignedIn: (email: string) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      onSignedIn((await api.login(email, password)).email);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="signin">
      <form className="signin-card" onSubmit={submit}>
        <p className="wordmark">BridgeOps</p>
        <h1>Sign in</h1>
        <p className="lede">Imports write to a real HubSpot account, so only people with an account here can run them.</p>
        <label className="field-label" htmlFor="email">Email</label>
        <input id="email" type="email" autoComplete="username" required value={email}
          onChange={(e) => setEmail(e.target.value)} />
        <label className="field-label" htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password}
          onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="banner banner-error" role="alert">{error}</p>}
        <button className="btn btn-primary" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="hint">No account yet? Create one on the server: <code>python scripts/users.py create you@example.com</code></p>
      </form>
    </main>
  );
}
