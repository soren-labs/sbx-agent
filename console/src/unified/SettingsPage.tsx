import { useState } from "react";
import { api, ApiError } from "../api/unified";
import { usePoll, useUnified } from "../state/unified";

export function SettingsPage() {
  const { state } = useUnified();
  const [label, setLabel] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { data, refresh } = usePoll(() => api.listApiKeys(), 10000, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const r = await api.createApiKey(label || "console");
      setCreated(r.key); // shown once — never stored
      setLabel("");
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.error.message : "failed");
    }
  };

  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
      </div>
      <section className="card settings-card">
        <h3>Account</h3>
        <div className="kv small">
          <span>email</span>
          <span>{state.user?.email}</span>
          <span>workspace</span>
          <span className="mono">{state.workspaceId}</span>
        </div>
      </section>
      <section className="card settings-card">
        <h3>API keys</h3>
        <form onSubmit={create} className="inline-act">
          <input
            aria-label="Key label"
            placeholder="label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <button className="btn btn-sm btn-primary" type="submit">
            Create key
          </button>
        </form>
        {created && (
          <p className="notice" role="status">
            Copy now — shown once: <code className="mono">{created}</code>
          </p>
        )}
        {error && <p className="notice">{error}</p>}
        <ul className="session-list">
          {(data?.items ?? []).map((k) => (
            <li className="session-meta-row" key={k.id}>
              <span>{k.label || k.id}</span>
              <button
                className="btn btn-sm btn-danger"
                onClick={() => void api.revokeApiKey(k.id).then(refresh)}
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
