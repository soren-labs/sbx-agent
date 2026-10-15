import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDown, Check, CircleSlash, RotateCcw, Square, TriangleAlert } from "lucide-react";
import type { ExecutorView, Message, Session, Turn } from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useApi } from "../../state/context";
import type { SessionLive, SessionLiveState } from "../../state/session-live";
import { Button } from "../../ui/Button";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Markdown } from "../../ui/Markdown";
import { Spinner } from "../../ui/Spinner";
import { useAction } from "../../ui/useAction";
import { when } from "../../ui/format";
import { harnessName } from "./harnesses";
import { FollowupComposer, type Outgoing } from "./FollowupComposer";
import { WorkGroup, clock, useElapsed } from "./work-items";
import {
  activityOf,
  blocksOf,
  entriesOf,
  formatDuration,
  formatTokens,
  isLiveTurn,
  secondsBetween,
  type TurnBlock,
} from "./worklog";

function UserMessage({ m }: { m: Message }) {
  const { t } = useI18n();
  const note = m.routing === "note";
  const body = m.parts.length ? m.parts.map((p) => p.content).join("\n\n") : m.content.map((c) => c.text ?? "").join("\n\n");
  return (
    <article className={`msg msg--user${note ? " msg--note" : ""}`} aria-label={t("conv.you")} data-message-id={m.id}>
      <div className="msg__bubble">
        {note ? <span className="msg__tag">{t("conv.note")}</span> : null}
        {body}
      </div>
      <time className="msg__time" title={when(m.created_at)}>
        {clock(m.created_at)}
      </time>
    </article>
  );
}

function OutgoingMessage({ out }: { out: Outgoing }) {
  const { t } = useI18n();
  return (
    <article className={`msg msg--user msg--outgoing${out.note ? " msg--note" : ""}`} aria-label={t("conv.you")} data-testid="outgoing">
      <div className="msg__bubble">
        {out.note ? <span className="msg__tag">{t("conv.note")}</span> : null}
        {out.text}
      </div>
      <span className="msg__time msg__time--visible">{t(out.messageId ? "conv.sent" : "conv.sending")}</span>
    </article>
  );
}

function TurnActions({ turn, live, ghost }: { turn: Turn; live: SessionLive; ghost?: boolean }) {
  const { t } = useI18n();
  const api = useApi();
  const cancel = useAction(async (key) => {
    await api.turns.cancel(turn.id, { idempotencyKey: key });
    live.refresh();
  });
  const retry = useAction(async (key) => {
    await api.turns.retry(turn.id, { idempotencyKey: key });
    live.refresh();
  });
  const ack = useAction(async (key) => {
    await api.turns.acknowledge(turn.id, { idempotencyKey: key });
    live.refresh();
  });
  if (!turn.actions.length) return null;
  const variant = ghost ? "ghost" : "secondary";
  return (
    <>
      <span className="turn-actions">
        {turn.actions.includes("cancel") ? (
          <Button size="sm" variant={variant} icon={<Square size={11} />} disabled={cancel.pending} onClick={() => void cancel.run()}>
            {t("conv.cancel_turn")}
          </Button>
        ) : null}
        {turn.actions.includes("retry") ? (
          <Button size="sm" variant={variant} icon={<RotateCcw size={12} />} disabled={retry.pending} onClick={() => void retry.run()}>
            {t("conv.retry_turn")}
          </Button>
        ) : null}
        {turn.actions.includes("acknowledge_unknown") ? (
          <Button size="sm" variant={variant} disabled={ack.pending} onClick={() => void ack.run()}>
            {t("conv.ack_unknown")}
          </Button>
        ) : null}
      </span>
      <ErrorNotice error={cancel.error ?? retry.error ?? ack.error} />
    </>
  );
}

/**
 * What the live Turn is doing right now, pinned above the composer: the real running
 * step, streaming state or lifecycle stage reported by the server — never a guess.
 */
function LiveBar({ turn, messages, live, now }: { turn: Turn; messages: Message[]; live: SessionLive; now: number }) {
  const { t } = useI18n();
  const elapsed = useElapsed(turn.started_at ?? turn.created_at, true);
  let label: string;
  let detail = "";
  if (turn.state === "running") {
    const doing = activityOf(messages.filter((m) => m.turn_id === turn.id), now);
    if (doing.type === "step") {
      label = t(`work.now.${doing.kind}` as I18nKey);
      detail = doing.detail;
    } else {
      label = t(doing.type === "thinking" ? "work.thinking" : doing.type === "writing" ? "work.now.writing" : "turn.running");
    }
  } else {
    label = t(turn.reason === "waiting_capacity" ? "turn.waiting_capacity" : turn.state === "queued" ? "turn.queued" : "turn.preparing");
  }
  return (
    <div className="livebar" role="status" data-state={turn.state} data-testid="turn-status">
      <span className="livebar__dot" aria-hidden="true" />
      <span className="livebar__label">{label}</span>
      {detail ? <code className="livebar__detail truncate">{detail}</code> : <span className="livebar__detail" />}
      {elapsed !== null ? <time className="livebar__time">{formatDuration(elapsed)}</time> : null}
      <TurnActions turn={turn} live={live} ghost />
    </div>
  );
}

/** How a finished Turn ended, with its real duration and usage. */
function TurnStatus({ turn, live, latest }: { turn: Turn; live: SessionLive; latest?: boolean }) {
  const { t } = useI18n();
  const took = secondsBetween(turn.started_at ?? turn.created_at, turn.finished_at);
  const usage = (turn.usage ?? null) as Record<string, unknown> | null;
  const tokensIn = usage ? Number(usage.input_tokens) || 0 : 0;
  const cached = usage ? Number(usage.cached_input_tokens) || 0 : 0;
  const tokensOut = usage ? Number(usage.output_tokens) || 0 : 0;

  if (turn.state === "succeeded") {
    return (
      <div className="turn-status turn-status--done" data-state={turn.state} data-testid="turn-status">
        <Check size={12} />
        <span className="turn-status__label">
          {took !== null ? t("turn.succeeded_in", { time: formatDuration(took) }) : t("turn.succeeded")}
        </span>
        {latest ? (
          // Absolute: the conversation also renders under /sessions/:id/:tab on desktop,
          // where a relative "changes" would resolve to /:tab/changes (no such route).
          <Link className="turn-status__link" to={`/sessions/${live.sessionId}/changes`}>
            {t("turn.view_changes")}
          </Link>
        ) : null}
        {usage ? (
          <span className="turn-status__usage" title={t("turn.usage_title", { cached: formatTokens(cached) })}>
            {t("turn.usage", { input: formatTokens(tokensIn + cached), output: formatTokens(tokensOut) })}
          </span>
        ) : null}
      </div>
    );
  }
  const reasonKey = `turn.reason.${turn.error?.code ?? turn.reason ?? ""}` as I18nKey;
  const explained = t(reasonKey) === reasonKey ? null : t(reasonKey);
  return (
    <div
      className={`turn-outcome ${turn.state === "cancelled" ? "turn-outcome--neutral" : "turn-outcome--error"}`}
      role={turn.state === "cancelled" ? "status" : "alert"}
      data-state={turn.state}
      data-testid="turn-status"
    >
      {turn.state === "cancelled" ? <CircleSlash size={14} /> : <TriangleAlert size={14} />}
      <div className="turn-outcome__body">
        <strong>{t(`turn.${turn.state}` as I18nKey)}</strong>
        {explained ? <p>{explained}</p> : null}
        {turn.error?.message ? <p className="turn-outcome__detail">{turn.error.message}</p> : null}
        {turn.error?.code || turn.reason ? (
          <p className="turn-outcome__code">
            <code>{turn.error?.code ?? turn.reason}</code>
            {took !== null ? ` · ${formatDuration(took)}` : ""}
          </p>
        ) : null}
        <TurnActions turn={turn} live={live} />
      </div>
    </div>
  );
}

function AgentBlock({ block, provider, live, latest, now }: { block: TurnBlock; provider: string; live: SessionLive; latest?: boolean; now: number }) {
  const { t } = useI18n();
  const running = isLiveTurn(block.turn ?? undefined);
  const replies = block.messages.filter((m) => m.role === "assistant");
  const entries = replies.flatMap((m) => entriesOf(m, running, now));
  const started = replies[0]?.created_at ?? block.turn?.started_at ?? block.turn?.created_at;
  // A live Turn with nothing to show yet is represented by the status bar alone.
  if (!entries.length && (!block.turn || running)) return null;
  return (
    <article className="msg msg--agent" aria-label={t("conv.agent")} aria-busy={running}>
      <div className="msg__byline">
        <span className="msg__avatar" aria-hidden="true">
          S
        </span>
        <strong>{harnessName(provider)}</strong>
        <time>{clock(started)}</time>
      </div>
      <div className="msg__body">
        {entries.map((entry) =>
          entry.type === "text" ? (
            <Markdown key={entry.key} text={entry.content} className={entry.streaming ? "assistant-message is-streaming" : "assistant-message"} />
          ) : (
            <WorkGroup key={entry.key} entry={entry} />
          ),
        )}
        {block.turn && !running ? <TurnStatus turn={block.turn} live={live} latest={latest} /> : null}
      </div>
    </article>
  );
}

/** Pixels from the bottom within which the log keeps following new output. */
const FOLLOW_SLACK = 80;

function Banners({ state, live }: { state: SessionLiveState; live: SessionLive }) {
  const { t } = useI18n();
  const api = useApi();
  const s = state.session;
  const activate = useAction(async (key) => {
    await api.sessions.activateExecutor(s!.id, { idempotencyKey: key });
    live.refresh(["session", "executor"]);
  });
  const unarchive = useAction(async (key) => {
    await api.sessions.unarchive(s!.id, { idempotencyKey: key });
    live.refresh(["session"]);
  });
  if (!s) return null;

  const exec: ExecutorView | null = state.executor;
  const leaseBad = exec?.leases.find((l) => l.state === "unavailable" || l.state === "quarantined");
  const wtBad = exec?.worktree && ["unavailable", "lost"].includes(exec.worktree.availability);
  const execTrouble = s.lifecycle === "open" && (leaseBad || wtBad);
  const diagnostic = leaseBad?.reason ?? (wtBad ? exec!.worktree.availability : "");

  return (
    <>
      {state.status === "reconnecting" ? (
        <div className="conv-banner conv-banner--info" role="status">
          <Spinner small />
          {t("session.reconnecting")}
        </div>
      ) : null}
      {execTrouble ? (
        <div className="conv-banner conv-banner--warn" role="status">
          <TriangleAlert size={14} />
          <span className="truncate">
            {t("error.executor_unavailable")}
            {diagnostic ? ` · ${diagnostic}` : ""}
          </span>
          <Button size="sm" variant="secondary" disabled={activate.pending} onClick={() => void activate.run()}>
            {t("session.activate_workspace")}
          </Button>
        </div>
      ) : null}
      {s.lifecycle === "closed" ? (
        <div className="conv-banner conv-banner--dim" role="status">
          {t("session.closed_banner")}
        </div>
      ) : null}
      {s.lifecycle === "archived" ? (
        <div className="conv-banner conv-banner--dim" role="status">
          <span className="truncate">{t("session.archived_banner")}</span>
          {s.actions.includes("unarchive") ? (
            <Button size="sm" variant="secondary" disabled={unarchive.pending} onClick={() => void unarchive.run()}>
              {t("session.unarchive")}
            </Button>
          ) : null}
        </div>
      ) : null}
      <ErrorNotice error={activate.error ?? unarchive.error} />
    </>
  );
}

export function ConversationPane({ session, live, state }: { session: Session; live: SessionLive; state: SessionLiveState }) {
  const { t } = useI18n();
  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [behind, setBehind] = useState(false);
  const [outgoing, setOutgoing] = useState<Outgoing | null>(null);
  const blocks = blocksOf(state.messages, state.turns);
  const provider = session.harness.provider_id;
  const liveTurn = state.turns.find((turn) => isLiveTurn(turn));
  // Streaming indicators are time-based: re-evaluate them while a Turn is live.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!liveTurn) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [!!liveTurn]);
  const clockNow = Math.max(now, state.messages.reduce((n, m) => m.parts.reduce((k, p) => Math.max(k, p.seen_at ?? 0), n), 0));
  // The optimistic bubble steps aside the moment the server's own Message is in the log.
  const pending = outgoing && !state.messages.some((m) => m.id === outgoing.messageId) ? outgoing : null;
  // Changes whenever anything visible in the log does.
  const revision =
    state.messages.reduce((n, m) => n + m.parts.reduce((k, p) => k + p.revision, 1), 0) +
    state.turns.reduce((n, turn) => n + turn.version, 0) +
    (pending ? 1 : 0);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    // Follow new output only while the reader is at the end; never yank them back
    // from history they scrolled up to read.
    if (following.current) el.scrollTop = el.scrollHeight;
    else setBehind(true);
  }, [revision]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const atEnd = el.scrollHeight - el.clientHeight - el.scrollTop < FOLLOW_SLACK;
    following.current = atEnd;
    if (atEnd) setBehind(false);
  };
  const jump = () => {
    const el = scroller.current;
    if (!el) return;
    following.current = true;
    setBehind(false);
    if (typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    else el.scrollTop = el.scrollHeight;
  };
  const onOutgoing = (out: Outgoing | null) => {
    // Sending is a deliberate return to the end of the conversation.
    if (out) following.current = true;
    setOutgoing(out);
  };

  return (
    <div className="conv">
      <Banners state={state} live={live} />
      <div className="conv__view">
        <div
          className="conv__scroll"
          ref={scroller}
          onScroll={onScroll}
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
          aria-label={t("session.conversation")}
          tabIndex={0}
        >
          <div className="conv__content">
            {blocks.map((block, index) => (
              <div className="conv__turn" key={block.turn?.id ?? `loose-${index}`} data-turn={block.turn?.ordinal}>
                {block.messages
                  .filter((m) => m.role !== "assistant")
                  .map((m) => (
                    <UserMessage key={m.id} m={m} />
                  ))}
                <AgentBlock block={block} provider={provider} live={live} latest={index === blocks.length - 1} now={clockNow} />
              </div>
            ))}
            {pending ? (
              <div className="conv__turn">
                <OutgoingMessage out={pending} />
              </div>
            ) : null}
            {!blocks.length && !pending && state.status !== "loading" ? <p className="conv__empty">{t("conv.empty")}</p> : null}
          </div>
        </div>
        {behind ? (
          <button type="button" className="conv__jump" onClick={jump} aria-label={t("conv.jump_latest")}>
            <ArrowDown size={13} />
            {t("conv.jump_latest")}
          </button>
        ) : null}
      </div>
      {liveTurn ? <LiveBar turn={liveTurn} messages={state.messages} live={live} now={clockNow} /> : null}
      <div className="conv__composer">
        <FollowupComposer sessionId={session.id} live={live} actions={session.actions} liveTurn={liveTurn} onOutgoing={onOutgoing} />
      </div>
    </div>
  );
}
