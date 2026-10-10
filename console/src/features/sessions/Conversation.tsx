import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { Markdown } from "../../components/Markdown";
import { Icon, Mark } from "../../components/icons";
import { ErrorNotice, useAction, type Tone } from "../../components/ui";
import type { Message, Turn } from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useApi } from "../../state/context";
import type { SessionLive, SessionLiveState } from "../../state/session-live";
import { harnessName } from "./harnesses";
import {
  LIVE_TAIL_ROWS,
  activityOf,
  blocksOf,
  countSteps,
  entriesOf,
  formatDuration,
  formatTokens,
  isLiveTurn,
  namesOf,
  previewOf,
  rowsOf,
  secondsBetween,
  spanOf,
  type Entry,
  type Row,
  type Step,
  type StepKind,
  type TurnBlock,
} from "./worklog";

export const turnTone = (s: string): Tone =>
  s === "succeeded" ? "ok" : s === "failed" || s === "interrupted" ? "err" : s === "cancelled" ? "dim" : s === "running" ? "run" : "warn";

type T = ReturnType<typeof useI18n>["t"];

const STEP_ICON: Record<StepKind, string> = {
  command: "terminal",
  edit: "file",
  read: "eye",
  search: "search",
  web: "globe",
  plan: "list",
  agent: "sparkle",
  thought: "sparkles",
  tool: "code",
};

function clock(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Seconds since `start`, ticking while `running`. */
function useElapsed(start: string | null | undefined, running: boolean): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);
  if (!start) return null;
  const ms = now - new Date(start).getTime();
  return Number.isFinite(ms) ? Math.max(0, Math.round(ms / 1000)) : null;
}

/** Bounded output: the first lines are visible, the rest is one click away. */
function Output({ label, value, tone }: { label: string; value: string; tone?: "error" }) {
  const { t } = useI18n();
  const [all, setAll] = useState(false);
  const { preview, hidden } = previewOf(value);
  return (
    <div className={`step-io ${tone === "error" ? "is-error" : ""}`}>
      <div className="step-io-label">{label}</div>
      <pre tabIndex={0}>{all ? value : preview}</pre>
      {hidden > 0 ? (
        <button type="button" className="step-more" aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? t("work.show_less") : t("work.show_more", { n: hidden })}
        </button>
      ) : null}
    </div>
  );
}

function StepDetail({ step }: { step: Step }) {
  const { t } = useI18n();
  // The command/path is already the row title; show raw input only when it adds something.
  const input = step.input && step.input.trim() !== step.detail ? step.input : "";
  return (
    <div className="step-body">
      {/* The exact command or path, never the shortened row title. */}
      {step.detail ? <Output label={step.name} value={step.detail} /> : null}
      {input ? <Output label={t("work.input")} value={input} /> : null}
      {step.output ? (
        <Output label={t("work.output")} value={step.output} tone={step.status === "error" ? "error" : undefined} />
      ) : step.status !== "running" ? (
        <p className="step-empty">{t("work.no_output")}</p>
      ) : null}
    </div>
  );
}

function StepState({ step }: { step: Step }) {
  const { t } = useI18n();
  if (step.status === "running") return <span className="work-ring" role="img" aria-label={t("work.step.running")} />;
  if (step.status === "error") return <span className="step-failed">{t("work.step.error")}</span>;
  return null;
}

/** One tool call: verb + the real command or path; the full input/output is a click away. */
function StepRow({ step, nested }: { step: Step; nested?: boolean }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const title = step.detail || step.name;
  return (
    <li className={`step step-${step.status} ${nested ? "is-nested" : ""}`} data-kind={step.kind} data-testid="work-step">
      <button type="button" className="step-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="step-icon" aria-hidden="true">
          <Icon name={STEP_ICON[step.kind]} size={13} />
        </span>
        <span className="step-kind">{t(`work.kind.${step.kind}` as I18nKey)}</span>
        <span className="step-title" title={title}>
          {title}
        </span>
        <StepState step={step} />
        <Icon name="chevron" size={12} className={`step-chevron ${open ? "open" : ""}`} />
      </button>
      {open ? <StepDetail step={step} /> : null}
    </li>
  );
}

/** Provider-visible reasoning: open while it streams, folded once the model moves on. */
function ThoughtRow({ step }: { step: Step }) {
  const { t } = useI18n();
  const [choice, setChoice] = useState<boolean | null>(null);
  const open = choice ?? !!step.streaming;
  const took = secondsBetween(step.startedAt, step.endedAt);
  const label = step.streaming
    ? t("work.thinking")
    : took ? t("work.thought_for", { time: formatDuration(took) }) : t("work.kind.thought");
  return (
    <li className={`step step-thought-row ${step.streaming ? "is-streaming" : ""}`} data-kind="thought" data-testid="work-step">
      <button type="button" className="step-head" aria-expanded={open} onClick={() => setChoice(!open)}>
        <span className="step-icon" aria-hidden="true">
          <Icon name={STEP_ICON.thought} size={13} />
        </span>
        <span className="step-kind">{label}</span>
        {open ? <span className="step-title" /> : <span className="step-title step-title-quiet">{step.output.trim().split("\n")[0]}</span>}
        <Icon name="chevron" size={12} className={`step-chevron ${open ? "open" : ""}`} />
      </button>
      {/* Reasoning is plain text from the model: not Markdown (it mangles __names__). */}
      {open ? <div className="step-body step-thought">{step.output.trim()}</div> : null}
    </li>
  );
}

/** Several finished steps of one kind on a single line; expanding lists every one. */
function FoldRow({ row }: { row: Row }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const names = namesOf(row);
  const label =
    row.kind === "tool"
      ? t("work.fold.tool", { n: row.steps.length, name: row.steps[0].name })
      : t(`work.fold.${row.kind}` as I18nKey, { n: row.kind === "edit" || row.kind === "read" ? names.length : row.steps.length });
  return (
    <li className="step step-fold" data-kind={row.kind} data-testid="work-fold">
      <button type="button" className="step-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="step-icon" aria-hidden="true">
          <Icon name={STEP_ICON[row.kind]} size={13} />
        </span>
        <span className="step-kind">{label}</span>
        <span className="step-title step-title-quiet" title={names.join(", ")}>
          {row.kind === "tool" ? "" : names.join(", ")}
        </span>
        <Icon name="chevron" size={12} className={`step-chevron ${open ? "open" : ""}`} />
      </button>
      {open ? (
        <ol className="work-steps is-nested">
          {row.steps.map((step) => (
            <StepRow key={step.key} step={step} nested />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

function RowItem({ row }: { row: Row }) {
  if (row.steps.length > 1) return <FoldRow row={row} />;
  return row.kind === "thought" ? <ThoughtRow step={row.steps[0]} /> : <StepRow step={row.steps[0]} />;
}

function plural(t: T, kind: StepKind, n: number): string {
  return t(`work.count.${kind}${n === 1 ? ".one" : ""}` as I18nKey, { n });
}

function summarize(t: T, steps: Step[]): string {
  const counts = countSteps(steps);
  const order: StepKind[] = ["command", "edit", "read", "search", "web", "plan", "agent", "tool"];
  return order
    .filter((kind) => counts[kind])
    .map((kind) => plural(t, kind, counts[kind]!))
    .join(" · ");
}

function WorkGroup({ entry }: { entry: Extract<Entry, { type: "work" }> }) {
  const { t } = useI18n();
  // Open while the agent is working here; the user's own toggle wins afterwards.
  const [choice, setChoice] = useState<boolean | null>(null);
  const [all, setAll] = useState(false);
  const failed = entry.steps.filter((s) => s.status === "error").length;
  const open = choice ?? entry.running;
  const span = spanOf(entry.steps);
  const elapsed = useElapsed(span.start, entry.running);
  const took = secondsBetween(span.start, span.end);
  // Reasoning with no tool around it is context, not work: a single quiet line.
  const solo = entry.steps.length === 1 && entry.steps[0].kind === "thought";
  if (solo) {
    return (
      <ol className="work-steps is-solo" data-testid="work-group">
        <ThoughtRow step={entry.steps[0]} />
      </ol>
    );
  }
  const rows = rowsOf(entry.steps);
  const hidden = entry.running && !all ? Math.max(0, rows.length - LIVE_TAIL_ROWS) : 0;
  const head = entry.running
    ? elapsed !== null ? t("work.working_for", { time: formatDuration(elapsed) }) : t("work.working")
    : took ? t("work.worked_for", { time: formatDuration(took) }) : t("work.worked");
  return (
    <section className={`work ${entry.running ? "is-running" : ""} ${open ? "is-open" : ""}`} data-testid="work-group">
      <button type="button" className="work-head" aria-expanded={open} onClick={() => setChoice(!open)}>
        <Icon name="chevron" size={13} className={`step-chevron ${open ? "open" : ""}`} />
        <strong>{head}</strong>
        <span className="work-summary">{summarize(t, entry.steps)}</span>
        {failed ? <span className="work-failed">{t("work.failed_steps", { n: failed })}</span> : null}
      </button>
      {open ? (
        <ol className="work-steps">
          {hidden ? (
            <li className="step">
              <button type="button" className="step-more" onClick={() => setAll(true)}>
                {t("work.earlier", { n: hidden })}
              </button>
            </li>
          ) : null}
          {rows.slice(hidden).map((row) => (
            <RowItem key={row.key} row={row} />
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function UserMessage({ m }: { m: Message }) {
  const { t } = useI18n();
  const note = m.routing === "note";
  const body = m.parts.length
    ? m.parts.map((p) => p.content).join("\n\n")
    : m.content.map((c) => c.text ?? "").join("\n\n");
  return (
    <article className={`wl-msg wl-user ${note ? "is-note" : ""}`} aria-label={t("conv.you")} data-message-id={m.id}>
      <div className="wl-meta">
        <strong>{m.role === "system" ? t("conv.system") : t("conv.you")}</strong>
        {note ? <span className="wl-tag">{t("conv.note")}</span> : null}
        <time>{clock(m.created_at)}</time>
      </div>
      <div className="wl-bubble">{body}</div>
    </article>
  );
}

/** A message the user just sent, shown at once while the server accepts it. */
export interface Outgoing {
  text: string;
  note: boolean;
  /** Set once accepted: the bubble yields to the server's Message with this id. */
  messageId?: string;
}

function OutgoingMessage({ out }: { out: Outgoing }) {
  const { t } = useI18n();
  return (
    <article className={`wl-msg wl-user is-outgoing ${out.note ? "is-note" : ""}`} aria-label={t("conv.you")} data-testid="outgoing">
      <div className="wl-meta">
        <strong>{t("conv.you")}</strong>
        <span className="wl-tag">{t(out.messageId ? "conv.sent" : "conv.sending")}</span>
      </div>
      <div className="wl-bubble">{out.text}</div>
    </article>
  );
}

function TurnActions({ turn, live }: { turn: Turn; live: SessionLive }) {
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
  return (
    <>
      <span className="turn-actions">
        {turn.actions.includes("cancel") ? (
          <button type="button" className="button" disabled={cancel.pending} onClick={() => void cancel.run()}>
            <Icon name="stop" size={11} />
            {t("conv.cancel_turn")}
          </button>
        ) : null}
        {turn.actions.includes("retry") ? (
          <button type="button" className="button" disabled={retry.pending} onClick={() => void retry.run()}>
            <Icon name="refresh" size={12} />
            {t("conv.retry_turn")}
          </button>
        ) : null}
        {turn.actions.includes("acknowledge_unknown") ? (
          <button type="button" className="button" disabled={ack.pending} onClick={() => void ack.run()}>
            {t("conv.ack_unknown")}
          </button>
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
    label = t(
      turn.reason === "waiting_capacity" ? "turn.waiting_capacity" : turn.state === "queued" ? "turn.queued" : "turn.preparing",
    );
  }
  return (
    <div className="wl-live" role="status" data-state={turn.state} data-testid="turn-status">
      <span className="work-ring" aria-hidden="true" />
      <span className="turn-label">{label}</span>
      {detail ? <code className="wl-live-detail">{detail}</code> : <span className="wl-live-detail" />}
      {elapsed !== null ? <time className="turn-time">{formatDuration(elapsed)}</time> : null}
      <TurnActions turn={turn} live={live} />
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
      <div className="turn-status is-done" data-state={turn.state} data-testid="turn-status">
        <Icon name="check" size={12} />
        <span className="turn-label">
          {took !== null ? t("turn.succeeded_in", { time: formatDuration(took) }) : t("turn.succeeded")}
        </span>
        {latest ? (
          <Link className="turn-link" to="changes">
            {t("turn.view_changes")}
          </Link>
        ) : null}
        {usage ? (
          <span className="turn-usage" title={t("turn.usage_title", { cached: formatTokens(cached) })}>
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
      className={`turn-outcome ${turn.state === "cancelled" ? "is-neutral" : "is-error"}`}
      role={turn.state === "cancelled" ? "status" : "alert"}
      data-state={turn.state}
      data-testid="turn-status"
    >
      <Icon name={turn.state === "cancelled" ? "stop" : "warn"} size={14} />
      <div className="turn-outcome-body">
        <strong>{t(`turn.${turn.state}` as I18nKey)}</strong>
        {explained ? <p>{explained}</p> : null}
        {turn.error?.message ? <p className="turn-detail">{turn.error.message}</p> : null}
        {turn.error?.code || turn.reason ? (
          <p className="turn-code">
            <code>{turn.error?.code ?? turn.reason}</code>
            {took !== null ? ` · ${formatDuration(took)}` : ""}
          </p>
        ) : null}
        <TurnActions turn={turn} live={live} />
      </div>
    </div>
  );
}

function AgentBlock({
  block,
  provider,
  live,
  latest,
  now,
}: {
  block: TurnBlock;
  provider: string;
  live: SessionLive;
  latest?: boolean;
  now: number;
}) {
  const { t } = useI18n();
  const running = isLiveTurn(block.turn ?? undefined);
  const replies = block.messages.filter((m) => m.role === "assistant");
  const entries = replies.flatMap((m) => entriesOf(m, running, now));
  const started = replies[0]?.created_at ?? block.turn?.started_at ?? block.turn?.created_at;
  // A live Turn with nothing to show yet is represented by the status bar alone.
  if (!entries.length && (!block.turn || running)) return null;
  return (
    <article className="wl-msg wl-agent" aria-label={t("conv.agent")} aria-busy={running}>
      <div className="wl-meta">
        <span className="wl-mark" aria-hidden="true">
          <Mark small />
        </span>
        <strong>{harnessName(provider)}</strong>
        <time>{clock(started)}</time>
      </div>
      <div className="wl-body">
        {entries.map((entry) =>
          entry.type === "text" ? (
            <Markdown
              key={entry.key}
              text={entry.content}
              className={entry.streaming ? "assistant-message is-streaming" : "assistant-message"}
            />
          ) : (
            <WorkGroup key={entry.key} entry={entry} />
          ),
        )}
        {block.turn && !running ? <TurnStatus turn={block.turn} live={live} latest={latest} /> : null}
      </div>
    </article>
  );
}

export function Composer({
  sessionId,
  live,
  actions,
  busy,
  onOutgoing,
}: {
  sessionId: string;
  live: SessionLive;
  actions: string[];
  /** A Turn is in flight: a sent message queues behind it. */
  busy?: boolean;
  /** Reports the message being sent so the log can show it immediately. */
  onOutgoing?: (out: Outgoing | null) => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [text, setText] = useState("");
  const [note, setNote] = useState(false);
  // What is on the wire; a retry resends exactly this, whatever was typed since.
  const sending = useRef<Outgoing | null>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const canSend = actions.includes("send");
  const canNote = canSend || actions.includes("note");
  const asNote = !canSend || note;
  const send = useAction(async (key) => {
    const out = sending.current;
    if (!out) return;
    onOutgoing?.(out);
    try {
      const accepted = await api.sessions.sendMessage(
        sessionId,
        { content: out.text, routing: out.note ? "note" : "queue" },
        { idempotencyKey: key },
      );
      sending.current = null;
      setText((current) => (current === out.text ? "" : current)); // a retried draft
      onOutgoing?.(accepted.message_id ? { ...out, messageId: accepted.message_id } : null);
      live.refresh();
    } catch (err) {
      // Not accepted: the words go back into the box (unless something new was typed).
      onOutgoing?.(null);
      setText((current) => current || out.text);
      throw err;
    }
  });

  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  if (!canNote) return <p className="wl-closed">{t("conv.closed")}</p>;
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const content = text.trim();
    if (!content || send.pending) return;
    sending.current = { text: content, note: asNote };
    setText("");
    void send.run();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter breaks the line; never while an IME is composing.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      e.currentTarget.blur();
    }
  };
  return (
    <form className="wl-composer" onSubmit={submit}>
      <ErrorNotice error={send.error} onRetry={() => void send.run()} retryLabel={t("conv.retry_send")} />
      <div className="wl-composer-box">
        <textarea
          ref={area}
          id="followup"
          rows={1}
          aria-label={t("conv.composer")}
          value={text}
          placeholder={t(asNote ? "conv.note_ph" : busy ? "conv.queue_ph" : "conv.followup_ph")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
        />
        <div className="wl-composer-tools">
          {canSend ? (
            <label className="wl-note-toggle">
              <input type="checkbox" checked={note} onChange={(e) => setNote(e.target.checked)} />
              <span>{t("conv.route_note")}</span>
            </label>
          ) : null}
          <span className="wl-composer-hint">{t("conv.enter_hint")}</span>
          <button
            type="submit"
            className="wl-send"
            aria-label={asNote ? t("conv.add_note") : t("conv.send")}
            title={asNote ? t("conv.add_note") : t("conv.send")}
            disabled={!text.trim() || send.pending}
          >
            <Icon name={send.pending ? "clock" : "arrow"} size={15} />
          </button>
        </div>
      </div>
    </form>
  );
}

/** Pixels from the bottom within which the log keeps following new output. */
const FOLLOW_SLACK = 80;

export function ConversationTab({ sessionId, live, state }: { sessionId: string; live: SessionLive; state: SessionLiveState }) {
  const { t } = useI18n();
  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [behind, setBehind] = useState(false);
  const [outgoing, setOutgoing] = useState<Outgoing | null>(null);
  const blocks = blocksOf(state.messages, state.turns);
  const provider = state.session?.harness.provider_id ?? "";
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
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };
  const onOutgoing = (out: Outgoing | null) => {
    // Sending is a deliberate return to the end of the conversation.
    if (out) following.current = true;
    setOutgoing(out);
  };

  return (
    <div className="worklog">
      <div className="worklog-view">
      <div
        className="worklog-scroll"
        ref={scroller}
        onScroll={onScroll}
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        aria-label={t("session.conversation")}
        tabIndex={0}
      >
        <div className="worklog-content">
          {blocks.map((block, index) => (
            <div className="wl-turn" key={block.turn?.id ?? `loose-${index}`} data-turn={block.turn?.ordinal}>
              {block.messages
                .filter((m) => m.role !== "assistant")
                .map((m) => (
                  <UserMessage key={m.id} m={m} />
                ))}
              <AgentBlock block={block} provider={provider} live={live} latest={index === blocks.length - 1} now={clockNow} />
            </div>
          ))}
          {pending ? (
            <div className="wl-turn">
              <OutgoingMessage out={pending} />
            </div>
          ) : null}
          {!blocks.length && !pending && state.status !== "loading" ? <p className="wl-empty">{t("conv.empty")}</p> : null}
        </div>
      </div>
      {behind ? (
        <button type="button" className="wl-jump" onClick={jump} aria-label={t("conv.jump_latest")} title={t("conv.jump_latest")}>
          <Icon name="down" size={14} />
        </button>
      ) : null}
      </div>
      {liveTurn ? <LiveBar turn={liveTurn} messages={state.messages} live={live} now={clockNow} /> : null}
      <Composer
        sessionId={sessionId}
        live={live}
        actions={state.session?.actions ?? []}
        busy={!!liveTurn}
        onOutgoing={onOutgoing}
      />
    </div>
  );
}
