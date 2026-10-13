import { Link } from "react-router-dom";
import type { ExecutorView, Session } from "../../../api/types";
import { useI18n } from "../../../i18n";
import { useApi } from "../../../state/context";
import type { SessionLive } from "../../../state/session-live";
import { Button } from "../../../ui/Button";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { useAction } from "../../../ui/useAction";
import { when } from "../../../ui/format";
import { harnessName } from "../harnesses";
import { AvailabilityPills } from "../status";
import { formatTokens } from "../worklog";
import type { SessionLiveState } from "../../../state/session-live";

function Card({ title, children, testId }: { title: string; children: React.ReactNode; testId?: string }) {
  return (
    <section className="ov-card" aria-label={title} role="region" data-testid={testId}>
      <h3 className="ov-card__title">{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function ExecutorCard({ session, executor, live }: { session: Session; executor: ExecutorView | null; live: SessionLive }) {
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
  // The server does not publish executor action eligibility (Session.actions only
  // covers send/archive/close/note), and closed Sessions cannot be activated at all
  // (control/application/execution.py raises invalid_transition). So: closed → no
  // buttons; a live lease (allocating/ready/quiescing — the rest are terminal:
  // released/lost) → Release only; anything else → Activate only.
  const LIVE_LEASES = ["allocating", "ready", "quiescing"];
  const closed = session.lifecycle === "closed";
  const hasLease = LIVE_LEASES.includes(session.executor.lease_state ?? "");

  return (
    <Card title={t("session.availability")}>
      <AvailabilityPills session={session} />
      <dl className="kv">
        <Row label={t("session.backend")}>{session.executor.backend}</Row>
        {wt ? <Row label={t("session.generation")}>{wt.generation}</Row> : null}
        <Row label={t("session.recovery_point")}>
          <span data-testid="recovery-point">
            {rp ? `${t("session.generation")} ${rp.generation} · ${when(rp.created_at)}` : t("session.no_recovery_point")}
          </span>
        </Row>
      </dl>
      <ErrorNotice error={activate.error ?? release.error} />
      {closed ? (
        <p className="faint small" data-testid="compute-closed">
          {t("session.compute_closed")}
        </p>
      ) : hasLease ? (
        <div className="ov-card__actions">
          <Button size="sm" variant="ghost" disabled={release.pending} onClick={() => void release.run()}>
            {t("session.release")}
          </Button>
        </div>
      ) : (
        <div className="ov-card__actions">
          <Button size="sm" variant="primary" disabled={activate.pending} onClick={() => void activate.run()}>
            {t("session.activate")}
          </Button>
        </div>
      )}
    </Card>
  );
}

export function OverviewTab({ session, state, live }: { session: Session; state: SessionLiveState; live: SessionLive }) {
  const { t } = useI18n();
  // Only what the CLI reported: Turns without usage contribute nothing and no cost is guessed.
  const reported = state.turns.filter((turn) => turn.usage);
  const tokens = (field: string) => reported.reduce((sum, turn) => sum + (Number((turn.usage as Record<string, unknown>)[field]) || 0), 0);
  return (
    <div className="ov">
      <Card title={t("session.details")}>
        <dl className="kv">
          <Row label={t("composer.harness")}>{harnessName(session.harness.provider_id)}</Row>
          <Row label={t("composer.model")}>{session.harness.model || "—"}</Row>
          {session.harness.effort ? <Row label={t("session.effort")}>{session.harness.effort}</Row> : null}
          <Row label={t("composer.compute")}>{session.executor.backend}</Row>
          <Row label={t("session.repository")}>
            {session.worktree?.repository ? (
              <span>
                {session.worktree.repository}
                {session.worktree.base_sha ? <span className="faint"> @ {session.worktree.base_sha.slice(0, 7)}</span> : null}
              </span>
            ) : (
              "—"
            )}
          </Row>
          {session.project_id ? <Row label={t("session.project")}>{session.project_id}</Row> : null}
          <Row label={t("session.created")}>{when(session.created_at)}</Row>
          {session.parent_session_id ? (
            <Row label={t("session.parent")}>
              <Link to={`/sessions/${session.parent_session_id}`}>{t("session.parent_open")}</Link>
            </Row>
          ) : null}
        </dl>
      </Card>

      <ExecutorCard session={session} executor={state.executor} live={live} />

      <Card title={t("session.usage")}>
        {reported.length ? (
          <dl className="kv" data-testid="session-usage">
            <Row label={t("session.usage.input")}>
              <span title={tokens("input_tokens").toLocaleString()}>{formatTokens(tokens("input_tokens"))}</span>
            </Row>
            <Row label={t("session.usage.cached")}>
              <span title={tokens("cached_input_tokens").toLocaleString()}>{formatTokens(tokens("cached_input_tokens"))}</span>
            </Row>
            <Row label={t("session.usage.output")}>
              <span title={tokens("output_tokens").toLocaleString()}>{formatTokens(tokens("output_tokens"))}</span>
            </Row>
            <Row label={t("session.usage.turns")}>
              {reported.length}/{state.turns.length}
            </Row>
          </dl>
        ) : (
          <p className="faint small">{t("session.usage.none")}</p>
        )}
      </Card>
    </div>
  );
}
