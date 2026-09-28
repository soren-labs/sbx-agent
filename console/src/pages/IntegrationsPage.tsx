import { useCallback, useEffect, useState } from "react";
import { isApiError, type ApiError } from "../api";
import type { IntegrationStatus, ProviderInfo } from "../api/types";
import { ErrorNotice } from "../components/ErrorNotice";
import { Icon, Spinner } from "../components/icons";
import { useI18n } from "../i18n";
import { useApi } from "../state/api";
import type { I18nKey } from "../i18n/en";

function readinessPill(
  p: ProviderInfo,
  t: (k: I18nKey, v?: Record<string, string | number>) => string,
) {
  switch (p.readiness) {
    case "ready":
      return <span className="pill pill-idle">{t("integrations.connected")}</span>;
    case "needs_login":
      return <span className="pill pill-failed">{t("integrations.needs_login")}</span>;
    case "busy":
      return <span className="pill pill-running">{t("integrations.busy")}</span>;
    case "disabled":
      return <span className="pill pill-ended">{t("integrations.disabled")}</span>;
    default:
      return <span className="pill pill-ended">{t("integrations.unhealthy")}</span>;
  }
}

export function IntegrationsPage() {
  const { t } = useI18n();
  const api = useApi();
  const [data, setData] = useState<IntegrationStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [connecting, setConnecting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getIntegrations());
    } catch (e) {
      setError(isApiError(e) ? e : null);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const connectGithub = async () => {
    setConnecting(true);
    setError(null);
    try {
      // POST /v1/github/app/authorize → {authorize_url} — open the GitHub
      // install/authorize flow in a new tab; the card refreshes on return.
      const { url } = await api.beginGithubAuthorize();
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      setError(isApiError(e) ? e : null);
    } finally {
      setConnecting(false);
    }
  };

  return (
    <div>
      <h1>{t("integrations.heading")}</h1>
      <ErrorNotice error={error} onRetry={load} onDismiss={() => setError(null)} />
      {loading || !data ? (
        <div className="empty"><Spinner /> {t("common.loading")}</div>
      ) : (
        <>
          <h2>{t("integrations.providers")}</h2>
          <div className="int-grid" data-testid="provider-cards">
            {data.providers.map((p) => (
              <div className="int-card card" key={p.id}>
                <div className="head">
                  <h2>{p.label}</h2>
                  {readinessPill(p, t)}
                </div>
                <div className="muted small">
                  {t("integrations.accounts", {
                    avail: p.accountsAvailable,
                    total: p.accountsTotal,
                  })}
                </div>
                {p.models.length > 0 && (
                  <div className="faint small" style={{ marginTop: 4 }}>
                    {p.models.join(" · ")}
                  </div>
                )}
                {p.runtimeStatus !== "ready" && (
                  <div className="faint small" style={{ marginTop: 4 }}>
                    {t("integrations.runtime_status", { status: p.runtimeStatus })}
                  </div>
                )}
                {p.accountsTotal === 0 && (
                  <div className="faint small" style={{ marginTop: 6 }}>
                    {t("integrations.no_accounts")}
                  </div>
                )}
              </div>
            ))}
          </div>

          <h2 style={{ marginTop: 22 }}>{t("integrations.github")}</h2>
          <div className="int-card card">
            <div className="head">
              <Icon name="github" size={18} />
              <h2>GitHub</h2>
              {data.github.connected ? (
                <span className="pill pill-idle">{t("integrations.connected")}</span>
              ) : (
                <span className="pill pill-failed">{t("integrations.not_connected")}</span>
              )}
            </div>
            <div className="muted small">{t("integrations.github_body")}</div>
            {data.github.accounts.length > 0 && (
              <div className="small" style={{ marginTop: 4 }}>
                {data.github.accounts.map((a) => (
                  <span key={a} className="mono" style={{ marginRight: 8 }}>{a}</span>
                ))}
              </div>
            )}
            {!data.github.configured && (
              <div className="faint small" style={{ marginTop: 6 }}>
                {t("integrations.github_unconfigured")}
              </div>
            )}
            {data.github.installable && !data.github.connected && (
              <div style={{ marginTop: 10 }}>
                <button
                  className="btn btn-sm"
                  disabled={connecting}
                  onClick={() => void connectGithub()}
                  data-testid="github-connect"
                >
                  {connecting ? <Spinner size={12} /> : null}
                  {t("integrations.connect_github")}
                </button>
              </div>
            )}
          </div>

          <h2 style={{ marginTop: 22 }}>{t("integrations.runtime")}</h2>
          <div className="int-card card">
            <div className="head">
              <h2>{t("integrations.runtime")}</h2>
              <span className={`pill ${data.runtime.enabled ? "pill-idle" : "pill-failed"}`}>
                {data.runtime.enabled ? t("integrations.enabled") : t("integrations.disabled")}
              </span>
            </div>
            {data.runtime.backend && (
              <div className="muted small">
                {t("integrations.runtime_body", { backend: data.runtime.backend })}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
