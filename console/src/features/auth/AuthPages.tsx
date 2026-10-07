import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";
import { isApiError } from "../../api/errors";
import { ErrorNotice, Field, Loading, useAction } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";
import { Brand } from "../../components/Brand";
import { Icon } from "../../components/icons";
import { useTheme } from "../../theme";
import { useAuth } from "../../state/auth";

function AuthCard({ title, children }: { title: string; children: ReactNode }) {
  const { t } = useI18n();
  useTheme();
  return (
    <main className="auth-page" id="main">
      <aside className="auth-story" aria-label={t("auth.workspace")}>
        <Brand />
        <div className="auth-story-body">
          <p className="eyebrow">{t("auth.workspace")}</p>
          <h2>{t("auth.hero")}</h2>
          <p className="auth-lead">{t("auth.hero_body")}</p>
          <ul className="auth-benefits">
            <li><Icon name="list" size={18} />{t("auth.benefit_sessions")}</li>
            <li><Icon name="github" size={18} />{t("auth.benefit_changes")}</li>
            <li><Icon name="plug" size={18} />{t("auth.benefit_connections")}</li>
          </ul>
        </div>
        <p className="auth-story-footer">{t("auth.tagline")}</p>
      </aside>
      <div className="auth-panel">
        <div className="auth-card">
          <div className="auth-mobile-brand"><Brand /></div>
          <p className="eyebrow">{t("auth.welcome")}</p>
          <h1>{title}</h1>
          <p className="auth-subtitle">{t("auth.subtitle")}</p>
          {children}
        </div>
        <p className="auth-note">{t("auth.security_note")}</p>
      </div>
    </main>
  );
}

export function LoginPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const api = useApi();
  const loc = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
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
    <AuthCard title={t("auth.login")}>
      <form onSubmit={onSubmit} aria-label={t("auth.login")}>
        <Field id="login-email" label={t("auth.email")}>
          <input id="login-email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field id="login-password" label={t("auth.password")}>
          <input id="login-password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <ErrorNotice error={loginError} />
        {unverified ? (
          <div className="row wrap small">
            <button type="button" className="btn btn-sm" disabled={resend.pending || !email} onClick={() => void resend.run().then((r) => setResent(Boolean(r)))}>
              {t("auth.resend")}
            </button>
            {resent ? <span role="status">{t("auth.resent")}</span> : null}
          </div>
        ) : null}
        <button type="submit" className="btn btn-primary" disabled={pending}>
          {t("auth.login")}
        </button>
      </form>
      <p className="muted small">
        {t("auth.no_account")} <Link to="/register">{t("auth.register")}</Link>
      </p>
    </AuthCard>
  );
}

export function RegisterPage() {
  const { t } = useI18n();
  const api = useApi();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
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
      <AuthCard title={t("auth.check_email")}>
        <p role="status">{t("auth.check_email_body", { email })}</p>
        <Link to="/login">{t("auth.login")}</Link>
      </AuthCard>
    );
  }
  return (
    <AuthCard title={t("auth.register")}>
      <form onSubmit={onSubmit} aria-label={t("auth.register")}>
        <Field id="reg-email" label={t("auth.email")}>
          <input id="reg-email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field id="reg-password" label={t("auth.password")} hint={t("auth.password_hint")}>
          <input id="reg-password" type="password" autoComplete="new-password" minLength={8} required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <ErrorNotice error={reg.error} />
        <button type="submit" className="btn btn-primary" disabled={reg.pending}>
          {t("auth.register")}
        </button>
      </form>
      <p className="muted small">
        {t("auth.have_account")} <Link to="/login">{t("auth.login")}</Link>
      </p>
    </AuthCard>
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
    <AuthCard title={t("auth.verify_title")}>
      {!token ? <p>{t("auth.verify_missing")}</p> : null}
      {state === "pending" ? <Loading /> : null}
      {state === "ok" ? <p role="status">{t("auth.verified")}</p> : null}
      <ErrorNotice error={verify.error} />
      <Link to="/login">{t("auth.login")}</Link>
    </AuthCard>
  );
}
