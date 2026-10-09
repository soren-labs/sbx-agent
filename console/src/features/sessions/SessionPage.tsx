import { Link, useNavigate, useParams } from "react-router-dom";
import type { ExecutorView, Session } from "../../api/types";
import { ErrorNotice, Loading, shortId, useAction, when } from "../../components/ui";
import { Icon } from "../../components/icons";
import { useI18n } from "../../i18n";
import { harnessName } from "./harnesses";
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
import { AvailabilityPills } from "./status";
import { TerminalTab } from "./TerminalTab";

const TABS = [
  "conversation",
  "activity",
  "changes",
  "files",
  "terminal",
  "services",
  "children",
] as const;
type Tab = (typeof TABS)[number];

function ExecutorPanel({
  session,
  executor,
  live,
}: {
  session: Session;
  executor: ExecutorView | null;
  live: SessionLive;
}) {
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
    <section
      className="card executor-panel hs-card"
      aria-label={t("session.availability")}
      role="region"
    >
      <h3>{t("session.availability")}</h3>
      <AvailabilityPills session={session} />
      <dl className="kv small" style={{ marginTop: 12 }}>
        <dt>{t("session.backend")}</dt>
        <dd>{session.executor.backend}</dd>
        {wt ? (
          <>
            <dt>{t("session.generation")}</dt>
            <dd>{wt.generation}</dd>
          </>
        ) : null}
        <dt>{t("session.recovery_point")}</dt>
        <dd data-testid="recovery-point">
          {rp ? `${t("session.generation")} ${rp.generation} · ${when(rp.created_at)}` : t("session.no_recovery_point")}
        </dd>
      </dl>
      <ErrorNotice error={activate.error ?? release.error} />
      <div className="hs-actions" style={{ marginTop: 12 }}>
        <button
          type="button"
          className="button"
          disabled={activate.pending}
          onClick={() => void activate.run()}
        >
          {t("session.activate")}
        </button>
        <button
          type="button"
          className="button ghost danger"
          disabled={release.pending}
          onClick={() => void release.run()}
        >
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
  const tab: Tab = (TABS as readonly string[]).includes(params.tab ?? "")
    ? (params.tab as Tab)
    : "conversation";
  const { live, state } = useSessionLive(id);
  const s = state.session;
  useDocumentTitle(s?.title || t("session.untitled"));

  const lifecycle = useAction(
    async (key, kind: "archive" | "unarchive" | "close") => {
      await api.sessions[kind](id, { idempotencyKey: key });
      live.refresh(["session"]);
    },
  );

  if (!s) {
    return (
      <div className="session-page-wrapper">
        <Link to="/sessions" className="back-link">
          <Icon name="back" size={12} />
          {t("session.back")}
        </Link>
        {state.status === "error" ? <ErrorNotice error={state.error} /> : <Loading />}
      </div>
    );
  }

  const active = s.lifecycle === "open" && s.activity === "running";
  const failed = s.activity === "failed";
  const statusLabel = t(
    active
      ? "home.status.working"
      : failed
        ? "home.status.attention"
        : s.lifecycle === "closed"
          ? "session.status.closed"
          : "home.status.idle",
  );
  const statusTone = active ? "ok" : failed ? "warn" : "idle";
  // Only what the CLI reported: Turns without usage contribute nothing and no cost is guessed.
  const reported = state.turns.filter((turn) => turn.usage);
  const tokens = (field: string) =>
    reported.reduce((sum, turn) => sum + (Number((turn.usage as Record<string, unknown>)[field]) || 0), 0);

  return (
    <div className="session-page-wrapper">
      <Link to="/sessions" className="back-link">
        <Icon name="back" size={13} />
        <span>{t("session.all")}</span>
      </Link>

      <div className="session-head-row">
        <div>
          <h1>{s.title || t("session.untitled")}</h1>
          <div className="session-meta-chips">
            <span className="session-provider-chip">
              <i className="provider-dot" />
              {harnessName(s.harness.provider_id)}
              {s.harness.model ? ` · ${s.harness.model}` : ""}
            </span>
            <span className="faint small" style={{ marginLeft: 8 }}>
              {s.role} · {shortId(s.id)}
            </span>
            {s.parent_session_id && (
              <Link to={`/sessions/${s.parent_session_id}`}>{t("session.parent")}</Link>
            )}
            {state.status === "reconnecting" && (
              <span role="status" className="faint small">
                {t("session.reconnecting")}
              </span>
            )}
          </div>
        </div>

        <div className="session-actions-right">
          <span className={`hs-badge ${statusTone}`}>
            <i /> {statusLabel}
          </span>
          {s.actions.includes("archive") && (
            <button
              type="button"
              className="button ghost"
              disabled={lifecycle.pending}
              onClick={() => void lifecycle.run("archive")}
            >
              {t("session.archive")}
            </button>
          )}
          {s.actions.includes("unarchive") && (
            <button
              type="button"
              className="button ghost"
              disabled={lifecycle.pending}
              onClick={() => void lifecycle.run("unarchive")}
            >
              {t("session.unarchive")}
            </button>
          )}
          {s.actions.includes("close") && (
            <button
              type="button"
              className="button ghost danger"
              disabled={lifecycle.pending}
              onClick={() => void lifecycle.run("close")}
            >
              {t("session.close")}
            </button>
          )}
        </div>
      </div>

      <ErrorNotice error={lifecycle.error} />

      <div className="session-workspace-split">
        <div className="session-main-pane">
          <nav className="pane-tabs" aria-label={t("session.sections")}>
            {TABS.map((x) => (
              <button
                key={x}
                type="button"
                className={tab === x ? "active" : ""}
                aria-current={tab === x ? "page" : undefined}
                onClick={() =>
                  nav(x === "conversation" ? `/sessions/${id}` : `/sessions/${id}/${x}`)
                }
              >
                {t(`tab.${x}` as I18nKey)}
              </button>
            ))}
          </nav>

          <div className="pane-body">
            {tab === "conversation" && (
              <ConversationTab sessionId={id} live={live} state={state} />
            )}
            {tab === "activity" && <ActivityTab state={state} />}
            {tab === "changes" && (
              <ChangesPanel
                sessionId={id}
                revision={state.revisions.changes + state.revisions.deliveries}
              />
            )}
            {tab === "files" && <FilesTab sessionId={id} />}
            {tab === "terminal" && <TerminalTab sessionId={id} />}
            {tab === "services" && (
              <ServicesTab sessionId={id} revision={state.revisions.services} />
            )}
            {tab === "children" && (
              <ChildrenTab
                sessionId={id}
                revision={state.revisions.delegations + state.revisions.changes}
              />
            )}
          </div>
        </div>

        <aside className="session-side-rail">
          <section className="card hs-card" aria-label={t("session.details")}>
            <h3>{t("session.details")}</h3>
            <table className="rail-table">
              <tbody>
                <tr>
                  <th>{t("composer.harness_label")}</th>
                  <td>{harnessName(s.harness.provider_id)}</td>
                </tr>
                <tr>
                  <th>{t("composer.model")}</th>
                  <td>{s.harness.model || "—"}</td>
                </tr>
                <tr>
                  <th>{t("composer.executor")}</th>
                  <td>{s.executor.backend}</td>
                </tr>
                <tr>
                  <th>{t("session.repository")}</th>
                  <td>{s.worktree?.repository || "—"}</td>
                </tr>
              </tbody>
            </table>
          </section>

          <section className="card hs-card" aria-label={t("session.usage")}>
            <h3>{t("session.usage")}</h3>
            {reported.length ? (
              <table className="rail-table" data-testid="session-usage">
                <tbody>
                  <tr>
                    <th>{t("session.usage.input")}</th>
                    <td>{tokens("input_tokens").toLocaleString()}</td>
                  </tr>
                  <tr>
                    <th>{t("session.usage.cached")}</th>
                    <td>{tokens("cached_input_tokens").toLocaleString()}</td>
                  </tr>
                  <tr>
                    <th>{t("session.usage.output")}</th>
                    <td>{tokens("output_tokens").toLocaleString()}</td>
                  </tr>
                  <tr>
                    <th>{t("session.usage.turns")}</th>
                    <td>
                      {reported.length}/{state.turns.length}
                    </td>
                  </tr>
                </tbody>
              </table>
            ) : (
              <p className="faint small">{t("session.usage.none")}</p>
            )}
          </section>

          <section className="card hs-card" aria-label={t("session.timeline")}>
            <h3>{t("session.timeline")}</h3>
            <table className="rail-table">
              <tbody>
                <tr>
                  <th>{t("session.turns")}</th>
                  <td>{state.turns.length}</td>
                </tr>
                <tr>
                  <th>{t("session.created")}</th>
                  <td>{when(s.created_at)}</td>
                </tr>
                <tr>
                  <th>{t("session.updated")}</th>
                  <td>{when(s.updated_at)}</td>
                </tr>
              </tbody>
            </table>
          </section>

          <ExecutorPanel session={s} executor={state.executor} live={live} />
        </aside>
      </div>
    </div>
  );
}
