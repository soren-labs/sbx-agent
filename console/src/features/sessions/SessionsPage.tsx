import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Session } from "../../api/types";
import { ErrorNotice, Empty, Loading } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useApi } from "../../state/context";
import { useDocumentTitle } from "../../state/title";
import { ActivityPill, AvailabilityPills } from "./status";

const LIFECYCLES = ["open", "archived", "closed", ""] as const;
const ROLES = ["", "developer", "review", "test", "research", "security", "integration"] as const;

export function SessionRow({ s }: { s: Session }) {
  const { t } = useI18n();
  return (
    <li className="session-row">
      <div className="sr-main">
        <Link to={`/sessions/${s.id}`} className="sr-title">
          {s.title || t("session.untitled")}
        </Link>
        <div className="faint small">
          {s.harness.provider_id}
          {s.harness.model ? ` / ${s.harness.model}` : ""} · {s.executor.backend} · {s.role}
        </div>
      </div>
      <div className="sr-side">
        <span className="row wrap">
          <ActivityPill activity={s.activity} />
          {s.lifecycle !== "open" ? <span className="faint small">{s.lifecycle}</span> : null}
        </span>
        <AvailabilityPills session={s} />
      </div>
    </li>
  );
}

export function useSessionList(filters: { lifecycle: string; role: string }, limit = 25) {
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id;
  const [items, setItems] = useState<Session[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(
    async (after: string | null) => {
      if (!w) return;
      setLoading(true);
      setError(null);
      try {
        const r = await api.sessions.list(w, { lifecycle: filters.lifecycle, role: filters.role, cursor: after, limit });
        setItems((prev) => (after ? [...prev, ...r.items] : r.items));
        setCursor(r.next_cursor);
      } catch (e) {
        setError(e);
      } finally {
        setLoading(false);
      }
    },
    [api, w, filters.lifecycle, filters.role, limit],
  );
  useEffect(() => {
    setItems([]);
    void load(null);
  }, [load]);
  return { items, cursor, loading, error, more: () => load(cursor), reload: () => load(null) };
}

export function SessionsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.sessions"));
  const [lifecycle, setLifecycle] = useState<string>("open");
  const [role, setRole] = useState("");
  const list = useSessionList({ lifecycle, role });
  return (
    <div>
      <div className="page-head">
        <h1>{t("nav.sessions")}</h1>
        <Link to="/" className="btn btn-primary btn-sm">
          {t("nav.new")}
        </Link>
      </div>
      <p className="page-lead muted">{t("sessions.intro")}</p>
      <div className="filter-chips" role="group" aria-label={t("sessions.filter_lifecycle")}>
        {LIFECYCLES.map((l) => (
          <button key={l || "all"} type="button" className={`chip${lifecycle === l ? " active" : ""}`} aria-pressed={lifecycle === l} onClick={() => setLifecycle(l)}>
            {l === "" ? t("sessions.all") : t(`lifecycle.${l}` as never)}
          </button>
        ))}
        <label className="sr-only" htmlFor="role-filter">
          {t("sessions.filter_role")}
        </label>
        <select id="role-filter" value={role} onChange={(e) => setRole(e.target.value)}>
          {ROLES.map((r) => (
            <option key={r || "all"} value={r}>
              {r === "" ? t("sessions.all_roles") : r}
            </option>
          ))}
        </select>
      </div>
      <ErrorNotice error={list.error} onRetry={list.reload} />
      {list.items.length ? (
        <ul className="session-list" aria-label={t("nav.sessions")}>
          {list.items.map((s) => (
            <SessionRow key={s.id} s={s} />
          ))}
        </ul>
      ) : null}
      {list.loading ? <Loading /> : null}
      {!list.loading && !list.items.length && !list.error ? <Empty>{t("sessions.empty")}</Empty> : null}
      {list.cursor && !list.loading ? (
        <button type="button" className="btn" onClick={() => void list.more()}>
          {t("common.load_more")}
        </button>
      ) : null}
    </div>
  );
}
