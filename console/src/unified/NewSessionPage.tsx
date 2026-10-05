/**
 * Session composer — OpenCode Zen model + manual GitHub + manual Modal,
 * no Codex anywhere. The model list comes from /api/models (real catalog
 * observed via the user's Zen connection, free models first).
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/unified";
import { usePoll, useUnified } from "../state/unified";

export function NewSessionPage() {
  const { state } = useUnified();
  const ws = state.workspaceId;
  const nav = useNavigate();
  const [repo, setRepo] = useState("");
  const [baseRef, setBaseRef] = useState("main");
  const [title, setTitle] = useState("");
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: models } = usePoll(
    () => (ws ? api.listModels(ws) : Promise.resolve({ items: [], default_model: null })),
    10000,
    [ws],
  );
  const usable = (models?.items ?? []).filter((m) => m.model);
  const freeFirst = [...usable].sort((a, b) => Number(b.free) - Number(a.free));
  const chosen = model || models?.default_model?.model || freeFirst[0]?.model || "";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ws) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.createSession(ws, {
        title: title || undefined,
        projectless_spec: repo
          ? { repository: repo, base_ref: baseRef }
          : undefined,
        harness: { provider_id: "opencode", model: chosen },
        message: prompt
          ? { routing: "queue", content: { text: prompt } }
          : undefined,
      });
      nav(`/sessions/${r.session.id}`);
    } catch (err) {
      setError(
        err instanceof ApiError ? `${err.error.code}: ${err.error.message}` : "failed",
      );
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <h1>New session</h1>
        <p className="muted">
          Harness: official OpenCode CLI against your Zen connection.
        </p>
      </div>
      <form className="card composer" onSubmit={submit}>
        <label className="field">
          <span>Title (optional)</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="field">
          <span>Repository (owner/name, optional)</span>
          <input
            placeholder="soren-labs/sbx-e2e-test"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
        </label>
        <label className="field">
          <span>Base ref</span>
          <input value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
        </label>
        <label className="field">
          <span>Model</span>
          <select value={chosen} onChange={(e) => setModel(e.target.value)} required>
            {freeFirst.length === 0 && <option value="">— no usable models —</option>}
            {freeFirst.map((m) => (
              <option key={m.id ?? m.model} value={m.model}>
                {m.model}
                {m.free ? " (free)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>First message (optional)</span>
          <textarea
            rows={5}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="What should the agent do?"
          />
        </label>
        {error && (
          <p className="notice" role="alert">
            {error}
          </p>
        )}
        <button className="btn btn-primary" disabled={busy || !chosen} type="submit">
          {busy ? "Creating…" : "Create session"}
        </button>
      </form>
    </>
  );
}
