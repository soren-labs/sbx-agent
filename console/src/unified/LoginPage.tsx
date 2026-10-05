import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/unified";
import { useUnified } from "../state/unified";

export function LoginPage() {
  const { signIn, signUp, state, ready } = useUnified();
  const nav = useNavigate();
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (ready && state.user) {
    nav("/sessions", { replace: true });
    return null;
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "signin") await signIn(email, password);
      else await signUp(email, password);
      nav("/sessions", { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError ? `${err.error.code}: ${err.error.message}` : "failed",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="main">
      <div className="main-inner narrow">
        <div className="page-head">
          <h1>sbx</h1>
          <p className="muted">Sign in with email and password.</p>
        </div>
        <form className="card" onSubmit={submit} aria-label="Sign in">
          <label className="field">
            <span>Email</span>
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Password</span>
            <input
              type="password"
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              required
              minLength={12}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && (
            <p className="notice" role="alert">
              {error}
            </p>
          )}
          <button className="btn btn-primary" disabled={busy} type="submit">
            {mode === "signin" ? "Sign in" : "Create account"}
          </button>
          <button
            className="btn btn-ghost"
            type="button"
            onClick={() => setMode(mode === "signin" ? "signup" : "signin")}
          >
            {mode === "signin" ? "Need an account? Sign up" : "Have an account? Sign in"}
          </button>
        </form>
      </div>
    </main>
  );
}
