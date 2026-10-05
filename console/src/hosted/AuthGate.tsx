import { createContext, useContext, useEffect, useState, type ReactNode, type FormEvent } from "react";
import { hostedRequest } from "./api";
import { Icon, Mark } from "../prototype/Icon";
import "./hosted.css";

export type HostedUser = {id: string; email: string};
const HostedUserContext = createContext<HostedUser | null>(null);
export const useHostedUser = () => useContext(HostedUserContext);
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
  const [showPassword, setShowPassword] = useState(false);
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
  const switchMode = () => {setStep(step === "login" ? "email" : "login"); setPassword(""); setCode(""); setGrant(""); setError("");};
  if (loading) return <div className="auth-splash" role="status" aria-label="Connecting"><Mark /><span className="auth-splash-bar" /></div>;
  if (user && location.pathname === "/auth") return <Navigate to={authReturnPath(location.search)} replace />;
  if (user) return <HostedUserContext.Provider value={user}><div key={user.id} className="auth-ready">{children}</div></HostedUserContext.Provider>;
  const registering = step !== "login";
  const stepIndex = step === "email" ? 0 : step === "code" ? 1 : 2;
  const titles = {login: "Sign in to SBX", email: "Create your account", code: "Verify your email", password: "Set your password"};
  const subtitles = {
    login: "Welcome back. Pick up where your agents left off.",
    email: "Start handing off coding work to agents in minutes.",
    code: `Enter the 6-digit code sent to ${email}.`,
    password: "Use at least 12 characters. You'll sign in with this next time.",
  };
  return <div className="auth-shell">
    <aside className="auth-brand" aria-hidden="true">
      <div className="auth-brand-top"><Mark /><span>SBX</span></div>
      <div className="auth-brand-copy">
        <p className="auth-kicker">Agent sessions in isolated sandboxes</p>
        <h2>Describe the change.<br />Review the pull request.</h2>
        <ul>
          <li><Icon name="shield" size={15} />Every Session runs in your own Modal sandbox</li>
          <li><Icon name="github" size={15} />Works on the repositories you connect</li>
          <li><Icon name="pr" size={15} />Delivers a reviewed draft PR, ready to merge</li>
        </ul>
      </div>
      <div className="auth-preview">
        <div className="auth-preview-head"><span className="status-dot pulse" />Fix session stream reconnect<small>Working</small></div>
        <div className="auth-preview-line"><Icon name="check" size={12} />Traced reconnect flow</div>
        <div className="auth-preview-line"><Icon name="check" size={12} />12 tests passed</div>
        <div className="auth-preview-line active"><Icon name="terminal" size={12} /><code>npm run build</code></div>
      </div>
    </aside>
    <main className="auth-panel">
      <div className="auth-card">
        <div className="auth-mobile-brand"><Mark small /><span>SBX</span></div>
        {registering && <ol className="auth-steps" aria-label="Registration progress">
          {["Email", "Verify", "Password"].map((label, i) => <li key={label} className={i < stepIndex ? "done" : i === stepIndex ? "current" : ""} aria-current={i === stepIndex ? "step" : undefined}>
            <span>{i < stepIndex ? <Icon name="check" size={11} /> : i + 1}</span>{label}
          </li>)}
        </ol>}
        <h1>{titles[step]}</h1>
        <p className="auth-subtitle">{subtitles[step]}</p>
        {import.meta.env.VITE_API_MODE === "mock" && step === "code" && <p className="auth-hint">Demo: any 6 digits work, e.g. 123456.</p>}
        {error && <p className="auth-error" role="alert"><Icon name="x" size={13} />{error}</p>}
        <form className="auth-form" onSubmit={event => void submit(event)}>
          {(step === "login" || step === "email") && <label className="auth-field">Email<input aria-label="Email" type="email" autoComplete="email" required autoFocus placeholder="you@company.com" value={email} onChange={e => setEmail(e.target.value)}/></label>}
          {step === "code" && <label className="auth-field">Verification code<input className="auth-code" aria-label="Verification code" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required autoFocus placeholder="000000" value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ""))}/></label>}
          {(step === "login" || step === "password") && <label className="auth-field">Password
            <span className="auth-password">
              <input aria-label="Password" type={showPassword ? "text" : "password"} autoComplete={step === "login" ? "current-password" : "new-password"} required minLength={step === "password" ? 12 : 1} maxLength={128} autoFocus={step === "password"} value={password} onChange={e => setPassword(e.target.value)}/>
              <button type="button" className="auth-reveal" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword} onClick={() => setShowPassword(v => !v)}><Icon name={showPassword ? "eyeOff" : "eye"} size={15} /></button>
            </span>
            {step === "password" && <span className={`auth-meter ${password.length >= 12 ? "ok" : ""}`}><i style={{width: `${Math.min(100, password.length / 12 * 100)}%`}} />{password.length >= 12 ? "Strong enough" : `${Math.max(0, 12 - password.length)} more characters`}</span>}
          </label>}
          <button className="auth-submit" disabled={busy}>{busy && <span className="auth-spinner" aria-hidden="true" />}{step === "login" ? "Sign in" : step === "email" ? "Send verification code" : step === "code" ? "Verify code" : "Set password"}</button>
        </form>
        {step === "code" && <button className="auth-link-button" disabled={busy || cooldown > 0} onClick={() => {setBusy(true); setError(""); void register().catch(e => setError(e.message)).finally(() => setBusy(false));}}>Resend code{cooldown ? ` (${cooldown}s)` : ""}</button>}
        <div className="auth-switch">
          <span>{step === "login" ? "New to SBX?" : "Already have an account?"}</span>
          <button className="auth-link-button" disabled={busy} onClick={switchMode}>{step === "login" ? "Create account" : "Back to sign in"}</button>
        </div>
      </div>
      <p className="auth-legal">Your provider credentials stay encrypted on the control plane and are never shown in the browser.</p>
    </main>
  </div>;
}
