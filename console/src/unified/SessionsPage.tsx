import { Link } from "react-router-dom";
import { api } from "../api/unified";
import { usePoll, useUnified } from "../state/unified";

export function SessionsPage() {
  const { state } = useUnified();
  const ws = state.workspaceId;
  const { data, error } = usePoll(
    () => (ws ? api.listSessions(ws) : Promise.resolve({ items: [] })),
    4000,
    [ws],
  );
  const items = data?.items ?? [];

  return (
    <>
      <div className="page-head">
        <h1>Sessions</h1>
        <Link className="btn btn-primary" to="/sessions/new">
          New session
        </Link>
      </div>
      {error && <p className="notice">{error.message}</p>}
      {items.length === 0 ? (
        <p className="empty">No sessions yet — start one.</p>
      ) : (
        <ul className="session-list">
          {items.map((s) => (
            <li key={s.id} className="session-row card">
              <Link to={`/sessions/${s.id}`} className="grow">
                <div className="session-meta-row">
                  <strong>{s.title || s.id}</strong>
                  <span className={`pill pill-${s.lifecycle}`}>{s.lifecycle}</span>
                </div>
                <div className="session-meta-row faint small">
                  <span>{s.harness.provider_id}</span>
                  <span className="mono">{s.harness.model}</span>
                  <span>
                    {String(s.projectless_spec?.repository ?? "no repo")}
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
