import { useEffect, useState, type ReactNode, type FormEvent } from "react";
import { hostedRequest } from "./api";
import { Navigate, useLocation } from "react-router-dom";

export function authReturnPath(search: string): string {
  const value = new URLSearchParams(search).get("returnTo") || "/";
  // Only existing product destinations. Reject schemes, protocol-relative
  // paths and backslashes (including encoded forms) and auth redirect loops.
  if (/[\\\\\u0000-\u0020]/.test(value) || !value.startsWith("/") || value.startsWith("//")) return "/";
  const url = new URL(value, "https://sbx.invalid");
  if (url.origin !== "https://sbx.invalid" || !/^\/(?:sessions\/[A-Za-z0-9_-]+|activity|review|settings|integrations(?:\/[a-z-]+)?)?\/?$/.test(url.pathname)) return "/";
  return url.pathname + url.search + url.hash;
}

export function AuthGate({children}: {children: ReactNode}) {
  const location = useLocation();
  const [user, setUser] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [step, setStep] = useState<"login" | "email" | "code" | "password">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState("");
  const [grant, setGrant] = useState("");
  const [cooldown, setCooldown] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {void hostedRequest("/auth/me").then(data => setUser(data.user)).catch(() => {}).finally(() => setLoading(false));}, []);
  useEffect(() => {
    if (!cooldown) return;
    const timer = window.setInterval(() => setCooldown(n => Math.max(0, n - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [Boolean(cooldown)]);
  const register = async () => {
    const data = await hostedRequest("/auth/register", {email});
    setChallenge(data.challenge_id); setCooldown(data.resend_after_s); setStep("code");
  };
  const submit = async (event?: FormEvent) => {
    event?.preventDefault(); setBusy(true); setError("");
    try {
      if (step === "email") await register();
      else if (step === "code") {
        const data = await hostedRequest("/auth/verify", {challenge_id: challenge, code});
        setGrant(data.registration_token); setCode(""); setStep("password");
      } else {
        const data = await hostedRequest(step === "login" ? "/auth/login" : "/auth/password", step === "login" ? {email, password} : {registration_token: grant, password});
        setUser(data.user); setPassword(""); setGrant(""); setChallenge("");
      }
    } catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  };
  if (loading) return <main className="settings-content" role="status">Connecting…</main>;
  if (user && location.pathname === "/auth") return <Navigate to={authReturnPath(location.search)} replace />;
  if (user) return <div key={user.id}>{children}</div>;
  return <main className="settings-content" style={{maxWidth: 440, margin: "8vh auto"}}>
    <h1>{step === "login" ? "Sign in to SBX" : step === "email" ? "Create your account" : step === "code" ? "Verify your email" : "Set your password"}</h1>
    {step === "code" && <p>Enter the 6-digit code sent to {email}.</p>}
    {error && <p role="alert">{error}</p>}
    <form onSubmit={event => void submit(event)}>
      {(step === "login" || step === "email") && <label className="form-label">Email<input aria-label="Email" type="email" autoComplete="email" required value={email} onChange={e => setEmail(e.target.value)}/></label>}
      {step === "code" && <label className="form-label">Verification code<input aria-label="Verification code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required value={code} onChange={e => setCode(e.target.value)}/></label>}
      {(step === "login" || step === "password") && <label className="form-label">Password<input aria-label="Password" type="password" autoComplete={step === "login" ? "current-password" : "new-password"} required minLength={step === "password" ? 12 : 1} maxLength={128} value={password} onChange={e => setPassword(e.target.value)}/></label>}
      <button className="button primary" disabled={busy}>{step === "login" ? "Sign in" : step === "email" ? "Send verification code" : step === "code" ? "Verify code" : "Set password"}</button>
    </form>
    {step === "code" && <button className="button" disabled={busy || cooldown > 0} onClick={() => {setBusy(true); setError(""); void register().catch(e => setError(e.message)).finally(() => setBusy(false));}}>Resend code{cooldown ? ` (${cooldown}s)` : ""}</button>}
    <button className="button" disabled={busy} onClick={() => {setStep(step === "login" ? "email" : "login"); setPassword(""); setCode(""); setGrant(""); setError("");}}>{step === "login" ? "Create account" : "Back to sign in"}</button>
  </main>;
}
