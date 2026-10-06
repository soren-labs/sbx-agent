import { Link, useNavigate, useParams } from "react-router-dom";
import type { ExecutorView, Session } from "../../api/types";
import { ErrorNotice, Loading, Pill, shortId, useAction, when } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useApi } from "../../state/context";
import type { SessionLive } from "../../state/session-live";
import { useSessionLive } from "../../state/use-session-live";
import { useDocumentTitle } from "../../state/title";
import { ChangesPanel } from "../changes/ChangesPanel";
import { ActivityTab } from "./ActivityTab";
import { ChildrenTab } from "./ChildrenTab";
import { ConversationTab } from "./Conversation";
import { FilesTab } from "./FilesTab";
import { ServicesTab } from "./ServicesTab";
import { ActivityPill, AvailabilityPills } from "./status";
import { TerminalTab } from "./TerminalTab";

const TABS = ["conversation", "activity", "changes", "files", "terminal", "services", "children"] as const;
type Tab = (typeof TABS)[number];

/** Compute availability and saved recovery point; independent of conversation outcome. */
function ExecutorPanel({ session, executor, live }: { session: Session; executor: ExecutorView | null; live: SessionLive }) {
  const { t } = useI18n();
  const api = useApi();
  const activate = useAction(async (key) => {
    await api.sessions.activateExecutor(session.id, { idempotencyKey: key });
    live.refresh(["session", "executor"]);
  });
  const release = useAction(async (key) => {
    await api.sessions.releaseExecutor(session.id, { idempotencyKey: key });
    live.refresh(["session", "executor"]);
  });
  const rp = executor?.recovery_point ?? null;
  const wt = executor?.worktree ?? session.worktree;
  return (
    <section className="card executor-panel" aria-label={t("session.availability")}>
      <h2>{t("session.availability")}</h2>
      <AvailabilityPills session={session} />
      <dl className="kv small">
        <dt>{t("session.backend")}</dt>
        <dd>{session.executor.backend}</dd>
        {wt ? (
          <>
            <dt>{t("session.generation")}</dt>
            <dd>{wt.generation}</dd>
          </>
        ) : null}
        <dt>{t("session.recovery_point")}</dt>
        <dd data-testid="recovery-point">{rp ? `${t("session.generation")} ${rp.generation} · ${when(rp.created_at)}` : t("session.no_recovery_point")}</dd>
      </dl>
      <ErrorNotice error={activate.error ?? release.error} />
      <div className="row wrap">
        <button type="button" className="btn btn-sm" disabled={activate.pending} onClick={() => void activate.run()}>
          {t("session.activate")}
        </button>
        <button type="button" className="btn btn-sm" disabled={release.pending} onClick={() => void release.run()}>
          {t("session.release")}
        </button>
      </div>
    </section>
  );
}

export function SessionPage() {
  const { t } = useI18n();
  const api = useApi();
  const nav = useNavigate();
  const params = useParams();
  const id = params.id!;
  const tab: Tab = (TABS as readonly string[]).includes(params.tab ?? "") ? (params.tab as Tab) : "conversation";
  const { live, state } = useSessionLive(id);
  const s = state.session;
  useDocumentTitle(s?.title || t("session.untitled"));

  const lifecycle = useAction(async (key, kind: "archive" | "unarchive" | "close") => {
    await api.sessions[kind](id, { idempotencyKey: key });
    live.refresh(["session"]);
  });

  if (!s) {
    return (
      <div>
        <Link to="/sessions" className="back-link">
          {t("session.back")}
        </Link>
        {state.status === "error" ? <ErrorNotice error={state.error} /> : <Loading />}
      </div>
    );
  }

  return (
    <div>
      <Link to="/sessions" className="back-link">
        {t("session.back")}
      </Link>
      <div className="session-head">
        <h1>{s.title || t("session.untitled")}</h1>
        <span className="session-actions">
          {s.actions.includes("archive") ? (
            <button type="button" className="btn btn-sm" disabled={lifecycle.pending} onClick={() => void lifecycle.run("archive")}>
              {t("session.archive")}
            </button>
          ) : null}
          {s.actions.includes("unarchive") ? (
            <button type="button" className="btn btn-sm" disabled={lifecycle.pending} onClick={() => void lifecycle.run("unarchive")}>
              {t("session.unarchive")}
            </button>
          ) : null}
          {s.actions.includes("close") ? (
            <button type="button" className="btn btn-sm btn-danger" disabled={lifecycle.pending} onClick={() => void lifecycle.run("close")}>
              {t("session.close")}
            </button>
          ) : null}
        </span>
      </div>
      <div className="session-meta-row">
        <span>
          {t("session.conversation_state")}: <ActivityPill activity={s.activity} />
        </span>
        <Pill tone={s.lifecycle === "open" ? "ok" : "dim"}>{s.lifecycle}</Pill>
        <span>
          {s.harness.provider_id}
          {s.harness.model ? ` / ${s.harness.model}` : ""}
        </span>
        <span className="faint">
          {s.role} · {shortId(s.id)}
        </span>
        {s.parent_session_id ? <Link to={`/sessions/${s.parent_session_id}`}>{t("session.parent")}</Link> : null}
        {state.status === "reconnecting" ? (
          <span role="status" className="faint">
            {t("session.reconnecting")}
          </span>
        ) : null}
      </div>
      <ErrorNotice error={lifecycle.error} />
      <div className="detail-grid">
        <div className="detail-main">
          <nav className="tabs" aria-label={t("session.sections")}>
            {TABS.map((x) => (
              <button
                key={x}
                type="button"
                className={tab === x ? "active" : ""}
                aria-current={tab === x ? "page" : undefined}
                onClick={() => nav(x === "conversation" ? `/sessions/${id}` : `/sessions/${id}/${x}`)}
              >
                {t(`tab.${x}` as I18nKey)}
              </button>
            ))}
          </nav>
          <div className="tab-body">
            {tab === "conversation" ? <ConversationTab sessionId={id} live={live} state={state} /> : null}
            {tab === "activity" ? <ActivityTab state={state} /> : null}
            {tab === "changes" ? <ChangesPanel sessionId={id} revision={state.revisions.changes + state.revisions.deliveries} /> : null}
            {tab === "files" ? <FilesTab sessionId={id} /> : null}
            {tab === "terminal" ? <TerminalTab sessionId={id} /> : null}
            {tab === "services" ? <ServicesTab sessionId={id} revision={state.revisions.services} /> : null}
            {tab === "children" ? <ChildrenTab sessionId={id} revision={state.revisions.delegations + state.revisions.changes} /> : null}
          </div>
        </div>
        <aside className="detail-rail">
          <ExecutorPanel session={s} executor={state.executor} live={live} />
        </aside>
      </div>
    </div>
  );
}
