import type { ActivityItem, Turn } from "../api/types";
import { useI18n } from "../i18n";
import { Icon } from "./icons";

const ACT_ICON: Record<string, string> = {
  status: "clock",
  message: "send",
  reasoning: "gear",
  command: "chev",
  file_change: "list",
  error: "warn",
  info: "plug",
};

/** One normalized activity row (Activity view + inline conversation markers). */
export function ActivityRow({ item }: { item: ActivityItem }) {
  const { t } = useI18n();
  let content: React.ReactNode = null;
  switch (item.kind) {
    case "status":
      content = t("act.status", { status: item.status ?? "" });
      break;
    case "reasoning":
      content = item.text;
      break;
    case "command":
      content = (
        <>
          <span className="mono grow">{item.command}</span>
          {item.exitCode !== undefined && (
            <span className={item.exitCode === 0 ? "exit-ok" : "exit-bad"}>
              exit {item.exitCode}
            </span>
          )}
        </>
      );
      break;
    case "file_change": {
      const rows =
        item.changes && item.changes.length > 0
          ? item.changes
          : item.path
            ? [{ path: item.path, kind: item.changeType ?? "modified" }]
            : [];
      content = (
        <>
          {rows.map((c) => (
            <span key={c.path} style={{ marginRight: 8 }}>
              <span className={c.kind}>{c.path}</span>{" "}
              <span className="faint">{c.kind}</span>
            </span>
          ))}
        </>
      );
      break;
    }
    case "error":
      content = (
        <>
          {item.error?.message ?? item.text}
          {item.error?.retryable ? ` (${t("act.retryable")})` : ""}
        </>
      );
      break;
    default:
      content = item.text ?? item.kind;
  }
  return (
    <div className={`act-row ${item.kind}`} data-kind={item.kind}>
      <span className="a-icon">
        <Icon name={ACT_ICON[item.kind] ?? "plug"} size={12} />
      </span>
      <span className="grow" style={{ minWidth: 0, overflowWrap: "break-word" }}>
        {content}
      </span>
    </div>
  );
}

/** Normalized Activity timeline — every event in order, including
 * session-level frames passed via ``extra``. */
export function ActivityTimeline({
  turns,
  extra = [],
}: {
  turns: Turn[];
  extra?: ActivityItem[];
}) {
  const { t } = useI18n();
  const items = [...extra, ...turns.flatMap((tn) => tn.activity)].sort(
    (a, b) => a.seq - b.seq,
  );
  if (items.length === 0) {
    return <div className="empty">{t("session.no_activity")}</div>;
  }
  return (
    <div data-testid="activity-timeline" style={{ padding: "12px 0" }}>
      {items.map((i) => (
        <ActivityRow key={i.id} item={i} />
      ))}
    </div>
  );
}

/**
 * Conversation view: user prompts + assistant messages, with activity
 * folded inline between them as slim markers.
 */
export function Conversation({ turns }: { turns: Turn[] }) {
  const { t } = useI18n();
  if (turns.length === 0) {
    return <div className="empty">{t("session.no_activity")}</div>;
  }
  return (
    <div className="convo" data-testid="conversation">
      {turns.map((turn) => {
        const nonMessage = turn.activity
          .filter((a) => a.kind !== "message")
          .sort((a, b) => a.seq - b.seq);
        const finalMsg =
          turn.result ??
          [...turn.activity]
            .filter((a) => a.kind === "message" && a.role === "assistant")
            .sort((a, b) => b.seq - a.seq)[0]?.text ??
          null;
        return (
          <div key={turn.id}>
            <div className="turn-sep">{t("session.turn", { n: turn.index })}</div>
            <div className="msg user">
              <div className="avatar" aria-hidden="true">Y</div>
              <div className="body">
                <div className="who">{t("session.you")}</div>
                <div className="text">{turn.prompt}</div>
              </div>
            </div>
            {nonMessage.length > 0 && (
              <div className="inline-act">
                {nonMessage.map((a) => (
                  <ActivityRow key={a.id} item={a} />
                ))}
              </div>
            )}
            {(finalMsg || turn.status === "running" || turn.status === "failed") && (
              <div className="msg">
                <div className="avatar" aria-hidden="true">A</div>
                <div className="body">
                  <div className="who">{t("session.assistant")}</div>
                  {finalMsg ? (
                    <div className="text">{finalMsg}</div>
                  ) : turn.status === "running" ? (
                    <div className="text muted">…</div>
                  ) : null}
                  {turn.error && (
                    <div className="act-row error" data-kind="error">
                      <span className="a-icon"><Icon name="warn" size={12} /></span>
                      {turn.error.message}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
