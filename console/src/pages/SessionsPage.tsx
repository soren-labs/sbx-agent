import { useCallback, useEffect, useMemo, useState } from "react";
import { isApiError, type ApiError } from "../api";
import type { Session } from "../api/types";
import { ErrorNotice } from "../components/ErrorNotice";
import { Spinner } from "../components/icons";
import { SessionCard } from "../components/SessionCard";
import { useI18n } from "../i18n";
import { useApi } from "../state/api";

const LIVE: Session["phase"][] = ["queued", "starting", "running", "idle"];

export function SessionsPage() {
  const { t } = useI18n();
  const api = useApi();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [filter, setFilter] = useState<"all" | "live" | "ended">("all");
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSessions(await api.listSessions());
    } catch (e) {
      setError(isApiError(e) ? e : null);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 15_000);
    return () => clearInterval(timer);
  }, [load]);

  const visible = useMemo(() => {
    let out = sessions;
    if (filter === "live") out = out.filter((s) => LIVE.includes(s.phase));
    if (filter === "ended")
      out = out.filter((s) => s.phase === "ended" || s.phase === "failed");
    const q = query.trim().toLowerCase();
    if (q) {
      out = out.filter(
        (s) =>
          s.title.toLowerCase().includes(q) ||
          s.repo?.name.toLowerCase().includes(q) ||
          s.provider.includes(q),
      );
    }
    return out;
  }, [sessions, filter, query]);

  return (
    <div>
      <h1>{t("sessions.heading")}</h1>
      <div className="row" style={{ marginBottom: 12 }}>
        <input
          className="grow"
          type="search"
          placeholder={t("sessions.search_ph")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label={t("sessions.search_ph")}
        />
      </div>
      <div className="filter-chips" role="tablist">
        {(["all", "live", "ended"] as const).map((f) => (
          <button
            key={f}
            role="tab"
            aria-selected={filter === f}
            className={`chip${filter === f ? " active" : ""}`}
            onClick={() => setFilter(f)}
          >
            {t(`sessions.filter.${f}` as const)}
          </button>
        ))}
      </div>
      <ErrorNotice error={error} onRetry={load} onDismiss={() => setError(null)} />
      {loading && sessions.length === 0 ? (
        <div className="empty"><Spinner /> {t("common.loading")}</div>
      ) : visible.length === 0 ? (
        <div className="empty">
          {query || filter !== "all"
            ? t("sessions.empty_search")
            : t("sessions.empty")}
        </div>
      ) : (
        <div className="recent-strip" data-testid="session-list">
          {visible.map((s) => (
            <SessionCard key={s.id} session={s} />
          ))}
        </div>
      )}
    </div>
  );
}
