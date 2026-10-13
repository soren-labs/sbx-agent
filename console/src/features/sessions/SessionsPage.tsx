import { Link } from "react-router-dom";
import { Plus, Rows3 } from "lucide-react";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useApi } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import { Button } from "../../ui/Button";
import { EmptyState } from "../../ui/EmptyState";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Pill, type Tone } from "../../ui/Pill";
import { Skeleton } from "../../ui/Skeleton";
import { formatRelative } from "../../ui/format";
import { PageHeader } from "../../shell/PageHeader";
import { harnessName } from "./harnesses";
import { activityLabelKey } from "./activity";
import type { Session } from "../../api/types";

const LABEL_TONE: Record<string, Tone> = {
  "status.running": "run",
  "status.queued": "info",
  "status.waiting": "warn",
  "status.attention": "err",
  "status.closed": "dim",
  "status.archived": "dim",
  "status.idle": "dim",
};

function Row({ s, locale }: { s: Session; locale: string }) {
  const { t } = useI18n();
  const key = activityLabelKey(s);
  return (
    <Link to={`/sessions/${s.id}`} className="sessions-row">
      <span className="sessions-row__title truncate">{s.title || t("sessions.untitled")}</span>
      <span className="sessions-row__status">
        <Pill tone={LABEL_TONE[key] ?? "dim"}>{t(key)}</Pill>
      </span>
      <span className="sessions-row__agent muted truncate">
        {harnessName(s.harness.provider_id)}
        {s.harness.model ? ` · ${s.harness.model}` : ""}
      </span>
      <span className="sessions-row__repo muted truncate">{s.worktree?.repository ?? "—"}</span>
      <span className="sessions-row__date faint">{formatRelative(s.created_at, locale)}</span>
    </Link>
  );
}

/** Flat list of the workspace's sessions; the sidebar shows the same data live. */
export function SessionsPage() {
  const { t, locale } = useI18n();
  useDocumentTitle(t("nav.sessions"));
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useQuery(w ? ["sessions", w, "page"] : null, () => api.sessions.list(w!, { limit: 50 }), { pollMs: 15000 });
  const items = q.data?.items ?? [];

  return (
    <>
      <PageHeader
        title={t("nav.sessions")}
        actions={
          <Link to="/">
            <Button variant="primary" size="sm" icon={<Plus size={14} />}>
              {t("nav.new")}
            </Button>
          </Link>
        }
      />
      <div className="page">
        <ErrorNotice error={q.error} onRetry={q.refetch} />
        {q.loading && !items.length ? (
          <div className="sessions-table">
            {Array.from({ length: 8 }, (_, i) => (
              <div key={i} className="sessions-row sessions-row--skeleton">
                <Skeleton width="60%" height={13} />
              </div>
            ))}
          </div>
        ) : items.length ? (
          <div className="sessions-table" role="table" aria-label={t("nav.sessions")}>
            <div className="sessions-row sessions-row--head" role="row">
              <span role="columnheader">{t("sessions.col.title")}</span>
              <span role="columnheader">{t("sessions.col.status")}</span>
              <span role="columnheader">{t("sessions.col.agent")}</span>
              <span role="columnheader">{t("sessions.col.repo")}</span>
              <span role="columnheader">{t("sessions.col.created")}</span>
            </div>
            {items.map((s) => (
              <Row key={s.id} s={s} locale={locale} />
            ))}
          </div>
        ) : (
          <EmptyState icon={<Rows3 size={22} />} title={t("sessions.empty")} />
        )}
      </div>
    </>
  );
}
