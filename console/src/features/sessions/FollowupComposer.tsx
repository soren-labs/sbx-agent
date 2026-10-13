import { useRef, useState } from "react";
import { ArrowUp, Square, StickyNote } from "lucide-react";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Spinner } from "../../ui/Spinner";
import { useToast } from "../../ui/Toast";
import { useAction } from "../../ui/useAction";
import { ComposerFrame } from "../composer/ComposerFrame";
import type { SessionLive } from "../../state/session-live";
import type { Turn } from "../../api/types";

/** A message the user just sent, shown at once while the server accepts it. */
export interface Outgoing {
  text: string;
  note: boolean;
  /** Set once accepted: the bubble yields to the server's Message with this id. */
  messageId?: string;
}

export function FollowupComposer({
  sessionId,
  live,
  actions,
  liveTurn,
  onOutgoing,
}: {
  sessionId: string;
  live: SessionLive;
  actions: string[];
  /** The Turn in flight: a sent message queues behind it; an empty box stops it. */
  liveTurn?: Turn | null;
  /** Reports the message being sent so the log can show it immediately. */
  onOutgoing?: (out: Outgoing | null) => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const [text, setText] = useState("");
  const [note, setNote] = useState(false);
  // What is on the wire; a retry resends exactly this, whatever was typed since.
  const sending = useRef<Outgoing | null>(null);
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
      toast({
        tone: "err",
        text: t("toast.send_failed"),
        action: { label: t("toast.retry"), onClick: () => void send.run() },
      });
      throw err;
    }
  });

  const stop = useAction(async (key) => {
    if (!liveTurn) return;
    await api.turns.cancel(liveTurn.id, { idempotencyKey: key });
    live.refresh();
  });

  if (!canNote) {
    return <p className="conv__closed">{t("conv.closed")}</p>;
  }

  const submit = () => {
    const content = text.trim();
    if (!content || send.pending) return;
    sending.current = { text: content, note: asNote };
    setText("");
    void send.run();
  };

  const empty = !text.trim();
  // Live Turn + empty box: the send button becomes Stop.
  const showStop = !!liveTurn && empty && liveTurn.actions.includes("cancel");

  return (
    <>
      <ComposerFrame
        label={t("conv.send_form")}
        inputLabel={t("conv.composer")}
        placeholder={t(asNote ? "conv.note_ph" : liveTurn ? "conv.queue_ph" : "conv.followup_ph")}
        value={text}
        onChange={setText}
        onSubmit={submit}
        toolbarLeft={
          canSend ? (
            <button
              type="button"
              className={`note-toggle${asNote ? " note-toggle--on" : ""}`}
              aria-pressed={asNote}
              onClick={() => setNote(!note)}
            >
              <StickyNote size={14} />
              {t("conv.route_note")}
            </button>
          ) : null
        }
        toolbarRight={
          showStop ? (
            <button
              type="button"
              className="composer__send composer__send--stop"
              aria-label={t("conv.cancel_turn")}
              title={t("conv.cancel_turn")}
              disabled={stop.pending}
              onClick={() => void stop.run()}
            >
              {stop.pending ? <Spinner small /> : <Square size={14} />}
            </button>
          ) : (
            <button
              type="submit"
              className="composer__send"
              aria-label={asNote ? t("conv.add_note") : t("conv.send")}
              title={asNote ? t("conv.add_note") : t("conv.send")}
              disabled={empty || send.pending}
            >
              {send.pending ? <Spinner small /> : <ArrowUp size={16} />}
            </button>
          )
        }
      />
      <ErrorNotice error={send.error ?? stop.error} onRetry={send.error ? () => void send.run() : undefined} retryLabel={t("conv.retry_send")} />
    </>
  );
}
