import { Link } from "react-router-dom";
import type { Session } from "../api/types";
import { ProviderBadge, StatusPill } from "./StatusPill";

function relTime(isoStr: string): string {
  const delta = Date.now() - new Date(isoStr).getTime();
  const m = Math.floor(delta / 60000);
  if (m < 1) return "now";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/**
 * Session entry in two shapes: a card for the home recent strip, and a
 * compact row for the Sessions index (one session per line — no
 * control-panel-style stacking).
 */
export function SessionCard({
  session,
  layout = "card",
}: {
  session: Session;
  layout?: "card" | "row";
}) {
  if (layout === "row") {
    return (
      <Link
        to={`/sessions/${session.id}`}
        className="session-row"
        data-testid={`session-card-${session.id}`}
      >
        <span className="sr-pill">
          <StatusPill phase={session.phase} endReason={session.endReason} />
        </span>
        <span className="sr-main">
          <span className="sr-title">{session.title}</span>
          {session.lastActivityPreview && (
            <span className="sr-preview">{session.lastActivityPreview}</span>
          )}
        </span>
        <span className="sr-side">
          <ProviderBadge provider={session.provider} model={session.model} />
          {session.repo && <span className="mono">{session.repo.name}</span>}
          <span className="faint">{relTime(session.updatedAt)}</span>
        </span>
      </Link>
    );
  }
  return (
    <Link
      to={`/sessions/${session.id}`}
      className="session-card card"
      data-testid={`session-card-${session.id}`}
    >
      <div className="title">{session.title}</div>
      <div className="meta">
        <StatusPill phase={session.phase} endReason={session.endReason} />
        <ProviderBadge provider={session.provider} model={session.model} />
        {session.repo && <span className="mono">{session.repo.name}</span>}
        <span className="faint" style={{ marginLeft: "auto" }}>
          {relTime(session.updatedAt)}
        </span>
      </div>
      {session.lastActivityPreview && (
        <div className="preview">{session.lastActivityPreview}</div>
      )}
    </Link>
  );
}
