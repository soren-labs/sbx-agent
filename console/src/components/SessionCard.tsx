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

export function SessionCard({ session }: { session: Session }) {
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
