import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";
import { isApiError } from "../../api/errors";
import { ErrorNotice, Loading, useAction } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";
import { Mark, Icon } from "../../components/icons";
import { useTheme } from "../../theme";
import { useAuth } from "../../state/auth";

interface AuthShellProps {
  title: string;
  subtitle: string;
  children: ReactNode;
  step?: "login" | "register" | "verify";
}

function AuthShell({ title, subtitle, children, step = "login" }: AuthShellProps) {
  const { t } = useI18n();
  useTheme();

  return (
    <div className="auth-shell" id="main">
      <aside className="auth-brand" aria-hidden="true">
        <div className="auth-brand-top">
          <Mark />
          <span>SBX Agent</span>
        </div>
        <div className="auth-brand-copy">
          <p className="auth-kicker">Agent sessions in isolated sandboxes</p>
          <h2>Describe the change.<br />Review the pull request.</h2>
          <ul>
            <li>
              <Icon name="shield" size={15} />
              Every Session runs in your own Modal sandbox
            </li>
            <li>
              <Icon name="branch" size={15} />
              Works on the repositories you connect
            </li>
            <li>
              <Icon name="pr" size={15} />
              Delivers a reviewed draft PR, ready to merge
            </li>
          </ul>
        </div>
        <div className="auth-preview">
          <div className="auth-preview-head">
            <span className="status-dot pulse" />
            Fix session stream reconnect
            <small>Working</small>
          </div>
          <div className="auth-preview-line">
            <Icon name="check" size={12} />
            Traced reconnect flow
          </div>
          <div className="auth-preview-line">
            <Icon name="check" size={12} />
            12 tests passed
          </div>
          <div className="auth-preview-line active">
            <Icon name="terminal" size={12} />
            <code>npm run build</code>
          </div>
        </div>
      </aside>
      <main className="auth-panel">
        <div className="auth-card">
          <div className="auth-mobile-brand">
            <Mark small />
            <span>SBX Agent</span>
          </div>
          {step === "register" && (
            <ol className="auth-steps" aria-label="Registration progress">
              <li className="current" aria-current="step">
                <span>1</span>Email
              </li>
              <li>
                <span>2</span>Verify
              </li>
              <li>
                <span>3</span>Password
              </li>
            </ol>
          )}
          <h1>{title}</h1>
          <p className="auth-subtitle">{subtitle}</p>
          {children}
        </div>
        <p className="auth-legal">{t("auth.security_note")}</p>
      </main>
    </div>
  );
}

export function LoginPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const api = useApi();
  const loc = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [unverified, setUnverified] = useState(false);
  const [resent, setResent] = useState(false);
  const [pending, setPending] = useState(false);
  const [loginError, setLoginError] = useState<unknown>(null);
  const resend = useAction((key) => api.auth.resendVerification(email, { idempotencyKey: key }));

  if (auth.status === "authenticated") {
    const from = (loc.state as { from?: string } | null)?.from ?? "/";
    return <Navigate to={from} replace />;
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setUnverified(false);
    setResent(false);
    setLoginError(null);
    setPending(true);
    const pw = password;
    setPassword(""); // write-only: never retained after submit
    try {
      await auth.login(email, pw);
    } catch (err) {
      if (isApiError(err) && /verif/i.test(`${err.code} ${err.message}`)) setUnverified(true);
      setLoginError(err);
    } finally {
      setPending(false);
    }
  };

  return (
    <AuthShell
      title="Sign in"
      subtitle={t("auth.subtitle")}
      step="login"
    >
      <form onSubmit={onSubmit} aria-label={t("auth.login")} className="auth-form">
        <label className="auth-field" htmlFor="login-email">
          {t("auth.email")}
          <input
            id="login-email"
            type="email"
            autoComplete="username"
            required
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="auth-field" htmlFor="login-password">
          {t("auth.password")}
          <span className="auth-password">
            <input
              id="login-password"
              type={showPassword ? "text" : "password"}
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button
              type="button"
              className="auth-reveal"
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((v) => !v)}
            >
              <Icon name={showPassword ? "eyeOff" : "eye"} size={15} />
            </button>
          </span>
        </label>
        <ErrorNotice error={loginError} />
        {unverified ? (
          <div className="row wrap small" style={{ marginBottom: 12 }}>
            <button
              type="button"
              className="auth-link-button"
              disabled={resend.pending || !email}
              onClick={() => void resend.run().then((r) => setResent(Boolean(r)))}
            >
              {t("auth.resend")}
            </button>
            {resent ? <span role="status">{t("auth.resent")}</span> : null}
          </div>
        ) : null}
        <button type="submit" className="auth-submit btn-primary" disabled={pending}>
          {pending && <span className="auth-spinner" aria-hidden="true" />}
          {t("auth.login")}
        </button>
      </form>
      <div className="auth-switch">
        <span>{t("auth.no_account")}</span>
        <Link to="/register" className="auth-link-button">
          {t("auth.register")}
        </Link>
      </div>
    </AuthShell>
  );
}

export function RegisterPage() {
  const { t } = useI18n();
  const api = useApi();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [done, setDone] = useState(false);
  const reg = useAction((key) => api.auth.register(email, password, { idempotencyKey: key }));

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await reg.run();
    setPassword("");
    if (r !== undefined) setDone(true);
  };

  if (done) {
    return (
      <AuthShell
        title={t("auth.check_email")}
        subtitle="Verification link sent."
        step="register"
      >
        <p role="status" className="auth-subtitle" style={{ marginTop: 12 }}>
          {t("auth.check_email_body", { email })}
        </p>
        <div className="auth-switch" style={{ marginTop: 20 }}>
          <Link to="/login" className="auth-link-button">
            {t("auth.login")}
          </Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={t("auth.register")}
      subtitle="Start handing off coding work to agents in minutes."
      step="register"
    >
      <form onSubmit={onSubmit} aria-label={t("auth.register")} className="auth-form">
        <label className="auth-field" htmlFor="reg-email">
          {t("auth.email")}
          <input
            id="reg-email"
            type="email"
            autoComplete="username"
            required
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="auth-field" htmlFor="reg-password">
          {t("auth.password")}
          <span className="auth-password">
            <input
              id="reg-password"
              type={showPassword ? "text" : "password"}
              autoComplete="new-password"
              minLength={8}
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button
              type="button"
              className="auth-reveal"
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((v) => !v)}
            >
              <Icon name={showPassword ? "eyeOff" : "eye"} size={15} />
            </button>
          </span>
        </label>
        <span className={`auth-meter ${password.length >= 8 ? "ok" : ""}`}>
          <i style={{ width: `${Math.min(100, (password.length / 8) * 100)}%` }} />
          {password.length >= 8 ? "Strong enough" : t("auth.password_hint")}
        </span>
        <ErrorNotice error={reg.error} />
        <button type="submit" className="auth-submit btn-primary" disabled={reg.pending}>
          {reg.pending && <span className="auth-spinner" aria-hidden="true" />}
          {t("auth.register")}
        </button>
      </form>
      <div className="auth-switch">
        <span>{t("auth.have_account")}</span>
        <Link to="/login" className="auth-link-button">
          {t("auth.login")}
        </Link>
      </div>
    </AuthShell>
  );
}

export function VerifyEmailPage() {
  const { t } = useI18n();
  const api = useApi();
  const [params] = useSearchParams();
  const token = params.get("token");
  const started = useRef(false);
  const [state, setState] = useState<"idle" | "pending" | "ok">(token ? "pending" : "idle");
  const verify = useAction((key) => api.auth.verifyEmail(token ?? "", { idempotencyKey: key }));
  const run = verify.run;

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    void run().then((r) => setState(r ? "ok" : "idle"));
  }, [token, run]);

  return (
    <AuthShell
      title={t("auth.verify_title")}
      subtitle="Complete your account verification."
      step="verify"
    >
      {!token ? <p className="auth-subtitle">{t("auth.verify_missing")}</p> : null}
      {state === "pending" ? <Loading /> : null}
      {state === "ok" ? (
        <p role="status" className="auth-subtitle" style={{ color: "var(--ok, #4ade80)", fontWeight: 550 }}>
          {t("auth.verified")}
        </p>
      ) : null}
      <ErrorNotice error={verify.error} />
      <div className="auth-switch" style={{ marginTop: 24 }}>
        <Link to="/login" className="auth-link-button">
          {t("auth.login")}
        </Link>
      </div>
    </AuthShell>
  );
}
