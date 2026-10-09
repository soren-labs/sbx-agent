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
  blocksOf,
  countSteps,
  entriesOf,
  formatDuration,
  formatTokens,
  isLiveTurn,
  previewOf,
  secondsBetween,
  type Entry,
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

function StepRow({ step }: { step: Step }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const title = step.kind === "thought" ? t("work.kind.thought") : step.detail || step.name;
  // The command/path is already the row title; show raw input only when it adds something.
  const input = step.input && step.input.trim() !== step.detail ? step.input : "";
  const expandable = step.kind === "thought" ? !!step.output : !!(input || step.output);
  return (
    <li className={`step step-${step.status}`} data-kind={step.kind}>
      <button
        type="button"
        className="step-head"
        aria-expanded={expandable ? open : undefined}
        disabled={!expandable}
        onClick={() => setOpen(!open)}
      >
        <span className="step-icon" aria-hidden="true">
          <Icon name={STEP_ICON[step.kind]} size={13} />
        </span>
        <span className="step-kind">{t(`work.kind.${step.kind}` as I18nKey)}</span>
        {step.kind !== "thought" ? (
          <span className="step-title" title={title}>
            {title}
          </span>
        ) : (
          <span className="step-title step-title-quiet">{step.output.trim().split("\n")[0]}</span>
        )}
        <span className="step-state">
          {step.status === "running" ? (
            <span className="work-ring" role="img" aria-label={t("work.step.running")} />
          ) : step.status === "error" ? (
            <span className="step-failed">{t("work.step.error")}</span>
          ) : (
            <Icon name="check" size={12} />
          )}
        </span>
        {expandable ? <Icon name="chevron" size={12} className={`step-chevron ${open ? "open" : ""}`} /> : null}
      </button>
      {open && expandable ? (
        <div className="step-body">
          {step.kind === "thought" ? (
            // Reasoning is plain text from the model: not Markdown (it mangles __names__).
            <div className="step-thought">{step.output.trim()}</div>
          ) : (
            <>
              {input ? <Output label={t("work.input")} value={input} /> : null}
              {step.output ? (
                <Output label={t("work.output")} value={step.output} tone={step.status === "error" ? "error" : undefined} />
              ) : step.status !== "running" ? (
                <p className="step-empty">{t("work.no_output")}</p>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </li>
  );
}

function plural(t: T, kind: StepKind, n: number): string {
  return t(`work.count.${kind}${n === 1 ? ".one" : ""}` as I18nKey, { n });
}

function summarize(t: T, steps: Step[]): string {
  const counts = countSteps(steps);
  const order: StepKind[] = ["command", "edit", "read", "search", "web", "plan", "agent", "tool"];
  const bits = order
    .filter((kind) => counts[kind])
    .map((kind) => plural(t, kind, counts[kind]!));
  return bits.join(" · ") || plural(t, "thought", counts.thought ?? 0);
}

function WorkGroup({ entry }: { entry: Extract<Entry, { type: "work" }> }) {
  const { t } = useI18n();
  // Open while the agent is working here; the user's own toggle wins afterwards.
  const [choice, setChoice] = useState<boolean | null>(null);
  const failed = entry.steps.filter((s) => s.status === "error").length;
  const open = choice ?? entry.running;
  const current = [...entry.steps].reverse().find((s) => s.status === "running") ?? entry.steps[entry.steps.length - 1];
  // Reasoning with no tool around it is context, not work: keep it visually quiet.
  const quiet = !entry.running && entry.steps.every((step) => step.kind === "thought");
  return (
    <section
      className={`work ${entry.running ? "is-running" : ""} ${open ? "is-open" : ""} ${quiet ? "is-quiet" : ""}`}
      data-testid="work-group"
    >
      <button type="button" className="work-head" aria-expanded={open} onClick={() => setChoice(!open)}>
        <Icon name="chevron" size={13} className={`step-chevron ${open ? "open" : ""}`} />
        <strong>{quiet ? t("work.kind.thought") : entry.running ? t("work.working") : t("work.worked")}</strong>
        <span className="work-summary">
          {quiet ? entry.steps[0].output.trim().split("\n")[0] : summarize(t, entry.steps)}
        </span>
        {failed ? <span className="work-failed">{t("work.failed_steps", { n: failed })}</span> : null}
        {entry.running ? <span className="work-ring" aria-hidden="true" /> : null}
      </button>
      {entry.running && !open && current ? (
        <div className="work-current">
          <Icon name={STEP_ICON[current.kind]} size={12} />
          <span>{current.kind === "thought" ? t("work.kind.thought") : current.detail || current.name}</span>
        </div>
      ) : null}
      {open ? (
        <ol className="work-steps">
          {entry.steps.map((step) => (
            <StepRow key={step.key} step={step} />
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

/** One line of truth about a Turn: what it is doing, or how it ended. */
function TurnStatus({ turn, live, latest }: { turn: Turn; live: SessionLive; latest?: boolean }) {
  const { t } = useI18n();
  const running = isLiveTurn(turn);
  const elapsed = useElapsed(turn.started_at ?? turn.created_at, running);
  const took = secondsBetween(turn.started_at ?? turn.created_at, turn.finished_at);
  const usage = (turn.usage ?? null) as Record<string, unknown> | null;
  const tokensIn = usage ? Number(usage.input_tokens) || 0 : 0;
  const cached = usage ? Number(usage.cached_input_tokens) || 0 : 0;
  const tokensOut = usage ? Number(usage.output_tokens) || 0 : 0;

  if (running) {
    const label =
      turn.state === "running"
        ? t("turn.running")
        : turn.reason === "waiting_capacity"
          ? t("turn.waiting_capacity")
          : turn.state === "queued"
            ? t("turn.queued")
            : t("turn.preparing");
    return (
      <div className="turn-status is-live" role="status" data-state={turn.state} data-testid="turn-status">
        <span className="work-ring" aria-hidden="true" />
        <span className="turn-label">{label}</span>
        {elapsed !== null ? <time className="turn-time">{formatDuration(elapsed)}</time> : null}
        <TurnActions turn={turn} live={live} />
      </div>
    );
  }
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
}: {
  block: TurnBlock;
  provider: string;
  live: SessionLive;
  latest?: boolean;
}) {
  const { t } = useI18n();
  const running = isLiveTurn(block.turn ?? undefined);
  const replies = block.messages.filter((m) => m.role === "assistant");
  const entries = replies.flatMap((m) => entriesOf(m, running));
  const started = replies[0]?.created_at ?? block.turn?.started_at ?? block.turn?.created_at;
  if (!block.turn && !entries.length) return null;
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
          entry.type === "text" ? <Markdown key={entry.key} text={entry.content} /> : <WorkGroup key={entry.key} entry={entry} />,
        )}
        {block.turn ? <TurnStatus turn={block.turn} live={live} latest={latest} /> : null}
      </div>
    </article>
  );
}

export function Composer({
  sessionId,
  live,
  actions,
  busy,
}: {
  sessionId: string;
  live: SessionLive;
  actions: string[];
  /** A Turn is in flight: a sent message queues behind it. */
  busy?: boolean;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [text, setText] = useState("");
  const [note, setNote] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const canSend = actions.includes("send");
  const canNote = canSend || actions.includes("note");
  const asNote = !canSend || note;
  const send = useAction(async (key) => {
    await api.sessions.sendMessage(
      sessionId,
      { content: text.trim(), routing: asNote ? "note" : "queue" },
      { idempotencyKey: key },
    );
    setText(""); // the draft survives until the server accepted the Message
    live.refresh();
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
    if (text.trim() && !send.pending) void send.run();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter breaks the line; never while an IME is composing.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
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
  const blocks = blocksOf(state.messages, state.turns);
  const provider = state.session?.harness.provider_id ?? "";
  const busy = state.turns.some((turn) => isLiveTurn(turn));
  // Changes whenever anything visible in the log does.
  const revision =
    state.messages.reduce((n, m) => n + m.parts.reduce((k, p) => k + p.revision, 1), 0) +
    state.turns.reduce((n, turn) => n + turn.version, 0);

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

  return (
    <div className="worklog">
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
              <AgentBlock block={block} provider={provider} live={live} latest={index === blocks.length - 1} />
            </div>
          ))}
          {!blocks.length && state.status !== "loading" ? <p className="wl-empty">{t("conv.empty")}</p> : null}
        </div>
      </div>
      {behind ? (
        <button type="button" className="wl-jump" onClick={jump}>
          <Icon name="down" size={12} />
          {t("conv.jump_latest")}
        </button>
      ) : null}
      <Composer sessionId={sessionId} live={live} actions={state.session?.actions ?? []} busy={busy} />
    </div>
  );
}
