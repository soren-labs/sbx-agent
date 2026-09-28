import { useCallback, useEffect, useState } from "react";
import { isApiError, type ApiError } from "../api";
import type { IntegrationStatus } from "../api/types";
import { ErrorNotice } from "../components/ErrorNotice";
import { Icon, Spinner } from "../components/icons";
import { useI18n } from "../i18n";
import { useApi } from "../state/api";

export function IntegrationsPage() {
  const { t } = useI18n();
  const api = useApi();
  const [data, setData] = useState<IntegrationStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

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
                  {p.needsLogin ? (
                    <span className="pill pill-failed">{t("integrations.needs_login")}</span>
                  ) : p.accountsAvailable > 0 ? (
                    <span className="pill pill-idle">{t("integrations.connected")}</span>
                  ) : (
                    <span className="pill pill-ended">{t("integrations.not_connected")}</span>
                  )}
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
                {p.accountsTotal === 0 && (
                  <div className="faint small" style={{ marginTop: 6 }}>
                    {t("integrations.no_accounts")}
                  </div>
                )}
                <div style={{ marginTop: 10 }}>
                  <button className="btn btn-sm" disabled={!p.needsLogin && p.accountsTotal > 0}>
                    {p.needsLogin ? t("integrations.connect") : t("integrations.reconnect")}
                  </button>
                </div>
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
            {data.github.account && (
              <div className="small" style={{ marginTop: 4 }}>
                <span className="mono">{data.github.account}</span>
              </div>
            )}
            {!data.github.connected && (
              <div style={{ marginTop: 10 }}>
                {data.github.installUrl ? (
                  <a className="btn btn-sm" href={data.github.installUrl}>
                    {t("integrations.connect")}
                  </a>
                ) : (
                  <button className="btn btn-sm">{t("integrations.connect")}</button>
                )}
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
