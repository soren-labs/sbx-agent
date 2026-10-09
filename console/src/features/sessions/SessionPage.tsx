import { Link, useNavigate, useParams } from "react-router-dom";
import type { ExecutorView, Session } from "../../api/types";
import { ErrorNotice, Loading, useAction, when } from "../../components/ui";
import { Icon } from "../../components/icons";
import { useI18n } from "../../i18n";
import { harnessName } from "./harnesses";
import { formatTokens, isLiveTurn } from "./worklog";
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

/** Workspace panels beside the conversation. "conversation" is the bare Session route. */
const PANELS = ["overview", "changes", "files", "terminal", "services", "children", "activity"] as const;
type Panel = (typeof PANELS)[number];

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
  const routed = params.tab ?? "conversation";
  const panel: Panel = (PANELS as readonly string[]).includes(routed) ? (routed as Panel) : "overview";
  // Narrow screens show one pane at a time; the route decides which.
  const showPanel = routed !== "conversation";
  const { live, state } = useSessionLive(id);
  const s = state.session;
  useDocumentTitle(s?.title || t("session.untitled"));

  const lifecycle = useAction(async (key, kind: "archive" | "unarchive" | "close") => {
    await api.sessions[kind](id, { idempotencyKey: key });
    live.refresh(["session"]);
  });

  if (!s) {
    return (
      <div className="session-page-wrapper">
        <Link to="/sessions" className="back-link">
          <Icon name="back" size={12} />
          {t("session.all")}
        </Link>
        {state.status === "error" ? <ErrorNotice error={state.error} /> : <Loading />}
      </div>
    );
  }

  const liveTurn = state.turns.find((turn) => isLiveTurn(turn));
  const activityKey = `activity.${s.activity}` as I18nKey;
  // The Turn is the most precise truth while one is in flight; otherwise the Session's activity.
  const statusLabel =
    s.lifecycle === "closed"
      ? t("session.status.closed")
      : liveTurn
        ? t(liveTurn.state === "running" ? "activity.running" : liveTurn.state === "queued" ? "activity.queued" : "turn.preparing_short")
        : t(activityKey) === activityKey
          ? s.activity.replaceAll("_", " ")
          : t(activityKey);
  const statusTone = liveTurn ? "run" : s.activity === "attention" ? "err" : s.activity === "awaiting_input" ? "ok" : "idle";
  // Only what the CLI reported: Turns without usage contribute nothing and no cost is guessed.
  const reported = state.turns.filter((turn) => turn.usage);
  const tokens = (field: string) =>
    reported.reduce((sum, turn) => sum + (Number((turn.usage as Record<string, unknown>)[field]) || 0), 0);
  const go = (next: Panel | "conversation") => nav(next === "conversation" ? `/sessions/${id}` : `/sessions/${id}/${next}`);

  return (
    <div className={`workbench-page ${showPanel ? "show-panel" : "show-chat"}`}>
      <header className="wb-head">
        <Link to="/sessions" className="wb-back" aria-label={t("session.all")} title={t("session.all")}>
          <Icon name="back" size={15} />
        </Link>
        <div className="wb-title">
          <h1 title={s.title || t("session.untitled")}>{s.title || t("session.untitled")}</h1>
          <div className="wb-sub">
            <span className="session-provider-chip">
              <i className="provider-dot" />
              {harnessName(s.harness.provider_id)}
              {s.harness.model ? ` · ${s.harness.model}` : ""}
            </span>
            {s.worktree?.repository ? (
              <span className="wb-repo">
                <Icon name="github" size={12} />
                {s.worktree.repository}
              </span>
            ) : null}
            {s.parent_session_id ? <Link to={`/sessions/${s.parent_session_id}`}>{t("session.parent")}</Link> : null}
          </div>
        </div>
        <div className="wb-actions">
          <span className={`hs-badge ${statusTone} ${liveTurn ? "is-live" : ""}`} data-testid="session-status">
            <i /> {statusLabel}
          </span>
          {s.actions.includes("archive") ? (
            <button type="button" className="button ghost" disabled={lifecycle.pending} onClick={() => void lifecycle.run("archive")}>
              {t("session.archive")}
            </button>
          ) : null}
          {s.actions.includes("unarchive") ? (
            <button type="button" className="button ghost" disabled={lifecycle.pending} onClick={() => void lifecycle.run("unarchive")}>
              {t("session.unarchive")}
            </button>
          ) : null}
          {s.actions.includes("close") ? (
            <button type="button" className="button ghost danger" disabled={lifecycle.pending} onClick={() => void lifecycle.run("close")}>
              {t("session.close")}
            </button>
          ) : null}
        </div>
      </header>

      {state.status === "reconnecting" ? (
        <div className="wb-banner" role="status">
          <span className="work-ring" aria-hidden="true" />
          {t("session.reconnecting")}
        </div>
      ) : null}
      <ErrorNotice error={lifecycle.error ?? (state.status === "error" ? state.error : null)} />

      <div className="wb-switch" role="tablist" aria-label={t("session.sections")}>
        <button type="button" role="tab" aria-selected={!showPanel} onClick={() => go("conversation")}>
          {t("tab.conversation")}
        </button>
        <button type="button" role="tab" aria-selected={showPanel} onClick={() => go(panel)}>
          {t("session.workspace")}
        </button>
      </div>

      <div className="wb-grid">
        <section className="wb-chat" aria-label={t("tab.conversation")}>
          <ConversationTab sessionId={id} live={live} state={state} />
        </section>

        <aside className="wb-panel" aria-label={t("session.workspace")}>
          <nav className="pane-tabs" aria-label={t("session.sections")}>
            {PANELS.map((x) => (
              <button
                key={x}
                type="button"
                className={panel === x ? "active" : ""}
                aria-current={panel === x ? "page" : undefined}
                onClick={() => go(x)}
              >
                {t(`tab.${x}` as I18nKey)}
              </button>
            ))}
          </nav>
          <div className="pane-body">
            {panel === "overview" && (
              <div className="session-side-rail">
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
                      <tr>
                        <th>{t("session.created")}</th>
                        <td>{when(s.created_at)}</td>
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
                          <td title={tokens("input_tokens").toLocaleString()}>{formatTokens(tokens("input_tokens"))}</td>
                        </tr>
                        <tr>
                          <th>{t("session.usage.cached")}</th>
                          <td title={tokens("cached_input_tokens").toLocaleString()}>
                            {formatTokens(tokens("cached_input_tokens"))}
                          </td>
                        </tr>
                        <tr>
                          <th>{t("session.usage.output")}</th>
                          <td title={tokens("output_tokens").toLocaleString()}>{formatTokens(tokens("output_tokens"))}</td>
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

                <ExecutorPanel session={s} executor={state.executor} live={live} />
              </div>
            )}
            {panel === "activity" && <ActivityTab state={state} />}
            {panel === "changes" && (
              <ChangesPanel sessionId={id} revision={state.revisions.changes + state.revisions.deliveries} />
            )}
            {panel === "files" && <FilesTab sessionId={id} />}
            {panel === "terminal" && <TerminalTab sessionId={id} />}
            {panel === "services" && <ServicesTab sessionId={id} revision={state.revisions.services} />}
            {panel === "children" && (
              <ChildrenTab sessionId={id} revision={state.revisions.delegations + state.revisions.changes} />
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
