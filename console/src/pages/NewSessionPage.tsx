import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { isApiError, type ApiError } from "../api";
import type { NewSessionInput, ProviderInfo, Session } from "../api/types";
import { Composer } from "../components/Composer";
import { ErrorNotice } from "../components/ErrorNotice";
import { Spinner } from "../components/icons";
import { SessionCard } from "../components/SessionCard";
import { useI18n } from "../i18n";
import { useApi } from "../state/api";

/** `/` — composer + recent sessions. Not a dashboard. */
export function NewSessionPage() {
  const { t } = useI18n();
  const api = useApi();
  const navigate = useNavigate();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [recent, setRecent] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [p, s] = await Promise.all([
        api.listProviders().catch(() => []),
        api.listSessions(),
      ]);
      setProviders(p);
      setRecent(s.slice(0, 6));
    } catch (e) {
      setError(isApiError(e) ? e : null);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async (input: NewSessionInput) => {
    setSubmitting(true);
    try {
      const session = await api.createSession(input);
      // Optimistic shell: navigate immediately; the detail page subscribes
      // to live queued/starting/running events from here.
      navigate(`/sessions/${session.id}`, { state: { session } });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <Composer
        providers={providers}
        submitting={submitting}
        onSubmit={submit}
      />
      <ErrorNotice error={error} onRetry={load} onDismiss={() => setError(null)} />
      <div className="row" style={{ marginTop: 26 }}>
        <h2 className="grow" style={{ margin: 0 }}>{t("composer.recent")}</h2>
        <Link to="/sessions" className="small">{t("composer.view_all")}</Link>
      </div>
      {loading ? (
        <div className="empty"><Spinner /> {t("common.loading")}</div>
      ) : recent.length === 0 ? (
        <div className="empty">{t("sessions.empty")}</div>
      ) : (
        <div className="recent-strip">
          {recent.map((s) => (
            <SessionCard key={s.id} session={s} />
          ))}
        </div>
      )}
    </div>
  );
}
