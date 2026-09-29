import { useState, type FormEvent } from "react";
import { isApiError, type ApiError } from "../api";
import type { SessionPhase } from "../api/types";
import { useI18n } from "../i18n";
import { ErrorNotice } from "./ErrorNotice";
import { Icon, Spinner } from "./icons";

interface Props {
  phase: SessionPhase;
  onSend: (text: string) => Promise<void> | void;
  onStop?: () => void;
}

/** Sticky follow-up composer pinned to the bottom of the session pane. */
export function FollowUp({ phase, onSend, onStop }: Props) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const ended = phase === "ended" || phase === "failed";

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const value = text.trim();
    if (!value || sending || ended) return;
    setSending(true);
    setError(null);
    try {
      await onSend(value);
      setText("");
    } catch (err) {
      setError(isApiError(err) ? err : null);
      if (!isApiError(err)) setError(null);
      throw err;
    } finally {
      setSending(false);
    }
  };

  if (ended) {
    return (
      <div className="followup-bar" data-testid="followup">
        <div className="notice warn" style={{ margin: "0 0 8px" }}>
          <div className="n-body">{t("session.ended_notice")}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="followup-bar" data-testid="followup">
      <ErrorNotice
        error={error}
        onRetry={() => submit()}
        onDismiss={() => setError(null)}
      />
      <form className="inner" onSubmit={submit}>
        <textarea
          aria-label={t("session.followup_ph")}
          placeholder={t("session.followup_ph")}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit();
            }
          }}
          disabled={sending}
          data-testid="followup-input"
        />
        {phase === "running" && onStop && (
          <button
            type="button"
            className="btn btn-danger"
            onClick={onStop}
            title={t("session.stop")}
          >
            <Icon name="stop" size={14} />
            <span className="sr-only">{t("session.stop")}</span>
          </button>
        )}
        <span className="faint small kbd-hint">{t("composer.send_hint")}</span>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={sending || !text.trim()}
          data-testid="followup-send"
        >
          {sending ? <Spinner size={14} /> : <Icon name="send" size={14} />}
          {t("session.send")}
        </button>
      </form>
    </div>
  );
}
