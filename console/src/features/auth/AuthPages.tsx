import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";
import { Moon, Sun } from "lucide-react";
import { isApiError } from "../../api/errors";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";
import { useAuth } from "../../state/auth";
import { useTheme } from "../../theme";
import { Button, IconButton } from "../../ui/Button";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Spinner } from "../../ui/Spinner";
import { useAction } from "../../ui/useAction";
import { Input } from "../../ui/Field";

function ThemeToggle() {
  const { resolved, setTheme } = useTheme();
  const { t } = useI18n();
  const next = resolved === "dark" ? "light" : "dark";
  return (
    <IconButton
      className="auth-theme-toggle"
      label={t(`theme.${next}` as const)}
      icon={next === "dark" ? <Moon size={15} /> : <Sun size={15} />}
      onClick={() => setTheme(next)}
    />
  );
}

function AuthShell({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  const { t } = useI18n();
  useTheme();
  return (
    <div className="auth" id="main">
      <ThemeToggle />
      <div className="auth__card">
        <div className="auth__brand">
          <span className="auth__mark">SBX</span>
          <span className="auth__product">{t("app.product")}</span>
        </div>
        <h1 className="auth__title">{title}</h1>
        {subtitle ? <p className="auth__subtitle">{subtitle}</p> : null}
        {children}
      </div>
      <p className="auth__note">{t("auth.security_note")}</p>
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
    <AuthShell title={t("auth.login")} subtitle={t("auth.subtitle")}>
      <form onSubmit={onSubmit} aria-label={t("auth.login")} className="auth__form">
        <label className="field" htmlFor="login-email">
          <span className="field__label">{t("auth.email")}</span>
          <Input
            id="login-email"
            type="email"
            autoComplete="username"
            required
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="field" htmlFor="login-password">
          <span className="field__label">{t("auth.password")}</span>
          <Input
            id="login-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <ErrorNotice error={loginError} title={t("auth.login_failed")} />
        {unverified ? (
          <div className="auth__resend">
            <Button
              size="sm"
              variant="ghost"
              disabled={resend.pending || !email}
              onClick={() => void resend.run().then((r) => setResent(Boolean(r)))}
            >
              {t("auth.resend")}
            </Button>
            {resent ? (
              <span className="small muted" role="status">
                {t("auth.resent")}
              </span>
            ) : null}
          </div>
        ) : null}
        <Button type="submit" variant="primary" loading={pending} className="auth__submit">
          {t("auth.login")}
        </Button>
      </form>
      <div className="auth__switch">
        <span className="muted">{t("auth.no_account")}</span>
        <Link to="/register" className="auth__link">
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
      <AuthShell title={t("auth.check_email")}>
        <p role="status" className="auth__subtitle">
          {t("auth.check_email_body", { email })}
        </p>
        <div className="auth__switch">
          <Link to="/login" className="auth__link">
            {t("auth.login")}
          </Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title={t("auth.register")} subtitle={t("auth.register_subtitle")}>
      <form onSubmit={onSubmit} aria-label={t("auth.register")} className="auth__form">
        <label className="field" htmlFor="reg-email">
          <span className="field__label">{t("auth.email")}</span>
          <Input
            id="reg-email"
            type="email"
            autoComplete="username"
            required
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="field" htmlFor="reg-password">
          <span className="field__label">{t("auth.password")}</span>
          <Input
            id="reg-password"
            type="password"
            autoComplete="new-password"
            minLength={8}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <span className="field__hint">{t("auth.password_hint")}</span>
        <ErrorNotice error={reg.error} />
        <Button type="submit" variant="primary" loading={reg.pending} className="auth__submit">
          {t("auth.register")}
        </Button>
      </form>
      <div className="auth__switch">
        <span className="muted">{t("auth.have_account")}</span>
        <Link to="/login" className="auth__link">
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
  const [email, setEmail] = useState("");
  const [resent, setResent] = useState(false);
  const verify = useAction((key) => api.auth.verifyEmail(token ?? "", { idempotencyKey: key }));
  const resend = useAction((key) => api.auth.resendVerification(email, { idempotencyKey: key }));
  const run = verify.run;

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    void run().then((r) => setState(r ? "ok" : "idle"));
  }, [token, run]);

  return (
    <AuthShell title={t("auth.verify_title")} subtitle={t("auth.verify_subtitle")}>
      {state === "pending" ? (
        <p className="auth__subtitle" role="status">
          <Spinner small /> {t("common.loading")}
        </p>
      ) : null}
      {state === "ok" ? (
        <p role="status" className="auth__verified">
          {t("auth.verified")}
        </p>
      ) : null}
      {state === "idle" ? (
        <>
          {!token ? <p className="auth__subtitle">{t("auth.verify_missing")}</p> : null}
          <ErrorNotice error={verify.error} />
          <form
            className="auth__form"
            aria-label={t("auth.resend")}
            onSubmit={(e) => {
              e.preventDefault();
              void resend.run().then((r) => setResent(Boolean(r)));
            }}
          >
            <label className="field" htmlFor="verify-email">
              <span className="field__label">{t("auth.email")}</span>
              <Input
                id="verify-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <Button type="submit" variant="secondary" loading={resend.pending}>
              {t("auth.resend")}
            </Button>
            {resent ? (
              <span className="small muted" role="status">
                {t("auth.resent")}
              </span>
            ) : null}
          </form>
        </>
      ) : null}
      <div className="auth__switch">
        <Link to="/login" className="auth__link">
          {t("auth.login")}
        </Link>
      </div>
    </AuthShell>
  );
}
