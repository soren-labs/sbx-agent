import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Plus, Rows3 } from "lucide-react";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { Field, Input, Select } from "../../ui/Field";
import { useDebounced } from "../../ui/useDebounced";
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

const PAGE = 50;
const SCOPES = ["open", "open,archived", "archived", "closed", "all"] as const;
type Scope = (typeof SCOPES)[number];

/**
 * Every Session of the workspace, newest activity first, in keyset pages. The first page
 * is polled; older pages are appended on demand and deduped by id, so a Session that
 * moves to the top while paging is neither listed twice nor lost.
 */
export function SessionsPage() {
  const { t, locale } = useI18n();
  useDocumentTitle(t("nav.sessions"));
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const [params, setParams] = useSearchParams();
  const [text, setText] = useState(params.get("q") ?? "");
  const search = useDebounced(text.trim(), 300);
  const scope = (SCOPES as readonly string[]).includes(params.get("lifecycle") ?? "") ? (params.get("lifecycle") as Scope) : "open";
  const filters = { ...(scope === "all" ? {} : { lifecycle: scope }), ...(search ? { q: search } : {}) };
  const q = useQuery(w ? ["sessions", w, "page", scope, search] : null, () => api.sessions.list(w!, { ...filters, limit: PAGE }), {
    pollMs: 15000,
  });
  const [older, setOlder] = useState<{ items: Session[]; cursor: string | null } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  useEffect(() => {
    setOlder(null);
    setMoreError(null);
  }, [w, scope, search]);
  useEffect(() => {
    const next = new URLSearchParams(params);
    if (search) next.set("q", search);
    else next.delete("q");
    if (next.toString() !== params.toString()) setParams(next, { replace: true });
  }, [search, params, setParams]);
  const first = q.data?.items ?? [];
  const seen = new Set(first.map((s) => s.id));
  const items = [...first, ...(older?.items ?? []).filter((s) => !seen.has(s.id))];
  const cursor = older ? older.cursor : (q.data?.next_cursor ?? null);
  const loadMore = async () => {
    if (!w || !cursor) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await api.sessions.list(w, { ...filters, limit: PAGE, cursor });
      setOlder((o) => ({ items: [...(o?.items ?? []), ...page.items], cursor: page.next_cursor }));
    } catch (e) {
      setMoreError(e);
    } finally {
      setLoadingMore(false);
    }
  };
  const setScope = (value: Scope) => {
    const next = new URLSearchParams(params);
    if (value === "open") next.delete("lifecycle");
    else next.set("lifecycle", value);
    setParams(next, { replace: true });
  };

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
        <div className="sessions-filters" role="search">
          <Field label={t("sessions.search")} htmlFor="sessions-search">
            <Input id="sessions-search" type="search" value={text} placeholder={t("sessions.search_ph")} onChange={(e) => setText(e.target.value)} />
          </Field>
          <Field label={t("sessions.scope")} htmlFor="sessions-scope">
            <Select id="sessions-scope" value={scope} onChange={(e) => setScope(e.target.value as Scope)}>
              {SCOPES.map((s) => (
                <option key={s} value={s}>
                  {t(`sessions.scope.${s.replace(",", "_")}` as I18nKey)}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <p className="faint small">{t("sessions.search_scope")}</p>
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
          <EmptyState icon={<Rows3 size={22} />} title={search ? t("sessions.search_none", { q: search }) : t("sessions.empty")} />
        )}
        <ErrorNotice error={moreError} onRetry={() => void loadMore()} />
        {cursor ? (
          <div className="sessions-more">
            <Button size="sm" variant="secondary" disabled={loadingMore} onClick={() => void loadMore()}>
              {t("sessions.load_more")}
            </Button>
            <span className="faint small">{t("sessions.shown", { n: items.length })}</span>
          </div>
        ) : null}
      </div>
    </>
  );
}
