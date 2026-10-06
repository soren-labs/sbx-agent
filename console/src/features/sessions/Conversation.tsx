import { useEffect, useRef, useState, type FormEvent } from "react";
import { Markdown } from "../../components/Markdown";
import { ErrorNotice, Field, Pill, useAction, type Tone } from "../../components/ui";
import type { Message, MessagePart, Turn } from "../../api/types";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";
import { messageText } from "../../state/session-events";
import type { SessionLive, SessionLiveState } from "../../state/session-live";

export const turnTone = (s: string): Tone =>
  s === "succeeded" ? "ok" : s === "failed" || s === "interrupted" ? "err" : s === "cancelled" ? "dim" : s === "running" ? "run" : "warn";

function ToolPart({ part }: { part: MessagePart }) {
  const d = (part.data ?? {}) as Record<string, unknown>;
  const input = d.input === undefined ? "" : typeof d.input === "string" ? d.input : JSON.stringify(d.input);
  const output = d.output === undefined ? "" : typeof d.output === "string" ? d.output : JSON.stringify(d.output);
  return (
    <details className="tool-part">
      <summary>
        <code>{String(d.name ?? "tool")}</code> <span className="faint">{String(d.title ?? "")}</span>{" "}
        <Pill tone={d.status === "completed" ? "ok" : d.status === "error" ? "err" : "run"}>{String(d.status ?? "pending")}</Pill>
      </summary>
      {input ? <pre>{input}</pre> : null}
      {output ? <pre>{output}</pre> : null}
    </details>
  );
}

export function MessageView({ m }: { m: Message }) {
  const { t } = useI18n();
  const own = m.role === "user";
  const who = own ? t("conv.you") : m.role === "system" ? t("conv.system") : t("conv.agent");
  const parts = m.parts.length ? m.parts : null;
  return (
    <article className={`msg ${own ? "user" : "agent"}`} aria-label={who} data-message-id={m.id}>
      <div className="avatar" aria-hidden="true">
        {who[0]}
      </div>
      <div className="body">
        <div className="who">
          {who}
          {m.routing === "note" ? <span className="faint"> · {t("conv.note")}</span> : null}
          {m.state === "streaming" ? <span className="faint"> · {t("conv.streaming")}</span> : null}
        </div>
        {parts ? (
          parts.map((p) =>
            p.kind === "tool" ? (
              <ToolPart key={p.key} part={p} />
            ) : p.kind === "reasoning" ? (
              <details key={p.key} className="reasoning">
                <summary>{t("conv.reasoning")}</summary>
                <Markdown text={p.content} />
              </details>
            ) : own ? (
              <div key={p.key} className="text">{p.content}</div>
            ) : (
              <Markdown key={p.key} text={p.content} />
            ),
          )
        ) : own ? (
          <div className="text">{messageText(m)}</div>
        ) : (
          <Markdown text={messageText(m)} />
        )}
      </div>
    </article>
  );
}

function TurnBanner({ turn, live }: { turn: Turn; live: SessionLive }) {
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
  return (
    <div className="turn-banner row wrap" role="group" aria-label={t("conv.turn", { n: turn.ordinal })}>
      <span>{t("conv.turn", { n: turn.ordinal })}</span>
      <Pill tone={turnTone(turn.state)}>{turn.state}</Pill>
      {turn.reason ? <span className="faint small">{turn.reason}</span> : null}
      {turn.error ? <span className="small">{turn.error.message || turn.error.code}</span> : null}
      {turn.actions.includes("cancel") ? (
        <button type="button" className="btn btn-sm" disabled={cancel.pending} onClick={() => void cancel.run()}>
          {t("conv.cancel_turn")}
        </button>
      ) : null}
      {turn.actions.includes("retry") ? (
        <button type="button" className="btn btn-sm" disabled={retry.pending} onClick={() => void retry.run()}>
          {t("conv.retry_turn")}
        </button>
      ) : null}
      {turn.actions.includes("acknowledge_unknown") ? (
        <button type="button" className="btn btn-sm" disabled={ack.pending} onClick={() => void ack.run()}>
          {t("conv.ack_unknown")}
        </button>
      ) : null}
      <ErrorNotice error={cancel.error ?? retry.error ?? ack.error} />
    </div>
  );
}

export function Composer({ sessionId, live, actions }: { sessionId: string; live: SessionLive; actions: string[] }) {
  const { t } = useI18n();
  const api = useApi();
  const [text, setText] = useState("");
  const [routing, setRouting] = useState<"queue" | "note">("queue");
  const canSend = actions.includes("send");
  const canNote = canSend || actions.includes("note");
  const effective = canSend ? routing : "note";
  const send = useAction(async (key) => {
    await api.sessions.sendMessage(sessionId, { content: text.trim(), routing: effective }, { idempotencyKey: key });
    setText(""); // draft survives until the server accepted the Message
    live.refresh();
  });
  if (!canNote) return <p className="muted small">{t("conv.closed")}</p>;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim()) void send.run();
  };
  return (
    <form className="followup" onSubmit={submit} aria-label={t("conv.composer")}>
      <Field id="followup" label={t("conv.composer")}>
        <textarea
          id="followup"
          rows={3}
          value={text}
          placeholder={t("conv.followup_ph")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit(e);
          }}
        />
      </Field>
      <ErrorNotice error={send.error} onRetry={() => void send.run()} retryLabel={t("conv.retry_send")} />
      <div className="row">
        {canSend ? (
          <select aria-label={t("conv.routing")} value={routing} onChange={(e) => setRouting(e.target.value as "queue" | "note")}>
            <option value="queue">{t("conv.route_queue")}</option>
            <option value="note">{t("conv.route_note")}</option>
          </select>
        ) : null}
        <span className="grow" />
        <button type="submit" className="btn btn-primary" disabled={!text.trim() || send.pending}>
          {effective === "note" ? t("conv.add_note") : t("conv.send")}
        </button>
      </div>
    </form>
  );
}

export function ConversationTab({ sessionId, live, state }: { sessionId: string; live: SessionLive; state: SessionLiveState }) {
  const { t } = useI18n();
  const end = useRef<HTMLDivElement>(null);
  const count = state.messages.length;
  const last = state.messages[count - 1];
  const lastRev = last ? last.parts.reduce((n, p) => n + p.revision, 0) : 0;
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [count, lastRev]);
  const attention = state.turns.filter((x) => x.actions.length > 0 || ["queued", "preparing", "running"].includes(x.state));
  return (
    <div>
      <div className="convo" role="log" aria-live="polite" aria-relevant="additions text" aria-label={t("session.conversation")}>
        {state.messages.map((m) => (
          <MessageView key={m.id} m={m} />
        ))}
        {!count && state.status !== "loading" ? <p className="muted">{t("conv.empty")}</p> : null}
        <div ref={end} />
      </div>
      {attention.map((turn) => (
        <TurnBanner key={turn.id} turn={turn} live={live} />
      ))}
      <Composer sessionId={sessionId} live={live} actions={state.session?.actions ?? []} />
    </div>
  );
}
