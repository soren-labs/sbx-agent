import { api, ApiError } from "../api/unified";
import { usePoll, useUnified } from "../state/unified";

export function ConnectionsPage() {
  const { state } = useUnified();
  const ws = state.workspaceId;
  const { data, refresh, error } = usePoll(
    () => (ws ? api.listConnections(ws) : Promise.resolve({ items: [] })),
    5000,
    [ws],
  );

  const disconnect = async (id: string) => {
    try {
      await api.disconnect(id);
      refresh();
    } catch (e) {
      alert(
        e instanceof ApiError ? `${e.error.code}: ${e.error.message}` : "failed",
      );
    }
  };

  const revalidate = async (id: string) => {
    try {
      await api.validateConnection(id);
      refresh();
    } catch (e) {
      alert(e instanceof ApiError ? e.error.message : "failed");
    }
  };

  return (
    <>
      <div className="page-head">
        <h1>Connections</h1>
        <p className="muted">
          Credential material is write-only and never shown here — only
          state, health, and observed capability.
        </p>
      </div>
      {error && <p className="notice">{error.message}</p>}
      {(data?.items ?? []).length === 0 ? (
        <p className="empty">
          No connections yet — visit <a href="/setup">Setup</a>.
        </p>
      ) : (
        <div className="int-grid">
          {(data?.items ?? []).map((c) => (
            <section className="card int-card" key={c.id}>
              <div className="section-head">
                <h3>{c.label || c.kind}</h3>
                <span className={`pill ${c.state === "configured" ? "pill-idle" : "pill-failed"}`}>
                  {c.state}/{c.health}
                </span>
              </div>
              <div className="kv small">
                <span>kind</span>
                <span className="mono">{c.kind}</span>
                <span>revocation_epoch</span>
                <span className="mono">{c.revocation_epoch}</span>
                <span>purposes</span>
                <span className="mono">{(c.allowed_purposes || []).join(", ")}</span>
              </div>
              <div className="inline-act">
                <button className="btn btn-sm" onClick={() => void revalidate(c.id)}>
                  Validate
                </button>
                {c.state !== "revoked" && (
                  <button
                    className="btn btn-sm btn-danger"
                    onClick={() => void disconnect(c.id)}
                  >
                    Disconnect
                  </button>
                )}
              </div>
            </section>
          ))}
        </div>
      )}
    </>
  );
}
