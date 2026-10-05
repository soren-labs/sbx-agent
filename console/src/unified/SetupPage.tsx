/**
 * Minimum setup checklist (RFC MVP): email/password is done by sign-in —
 * what remains is OpenCode Zen key + Modal token pair + GitHub token.
 * Secrets are write-only inputs; never echoed or persisted client-side.
 */

import { useState } from "react";
import { api, ApiError, type Connection } from "../api/unified";
import { usePoll, useUnified } from "../state/unified";

const STEPS: {
  kind: string;
  title: string;
  format: string;
  fields: { key: string; label: string; secret: boolean }[];
}[] = [
  {
    kind: "opencode_zen",
    title: "OpenCode Zen API key",
    format: "api_key",
    fields: [{ key: "api_key", label: "API key", secret: true }],
  },
  {
    kind: "modal",
    title: "Modal token ID + secret",
    format: "token_pair",
    fields: [
      { key: "token_id", label: "Token ID", secret: true },
      { key: "token_secret", label: "Token secret", secret: true },
    ],
  },
  {
    kind: "github",
    title: "GitHub token",
    format: "personal_token",
    fields: [{ key: "token", label: "Token", secret: true }],
  },
];

function StepCard({ step, existing }: { step: (typeof STEPS)[0]; existing?: Connection }) {
  const { state } = useUnified();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ws = state.workspaceId;

  const connected = !!existing && existing.state === "configured";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ws) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const conn = await api.createConnection(
        ws,
        step.kind,
        { format: step.format, payload: values },
        step.title,
      );
      await api.validateConnection(conn.id);
      setValues({});
      setNote("stored — validation job queued");
    } catch (err) {
      setError(err instanceof ApiError ? err.error.message : "failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card int-card" aria-label={step.title}>
      <div className="section-head">
        <h3>{step.title}</h3>
        <span className={`pill ${connected ? "pill-idle" : "pill-queued"}`}>
          {existing ? `${existing.state}/${existing.health}` : "missing"}
        </span>
      </div>
      {connected ? (
        <p className="muted small">
          configured{existing.external_identity?.provider
            ? ` — ${String(existing.external_identity.provider)}`
            : ""}
        </p>
      ) : (
        <form onSubmit={submit}>
          {step.fields.map((f) => (
            <label className="field" key={f.key}>
              <span>{f.label}</span>
              <input
                type={f.secret ? "password" : "text"}
                autoComplete="off"
                required
                value={values[f.key] ?? ""}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [f.key]: e.target.value }))
                }
              />
            </label>
          ))}
          <button className="btn btn-primary btn-sm" disabled={busy} type="submit">
            Store & validate
          </button>
        </form>
      )}
      {note && <p className="faint small">{note}</p>}
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

export function SetupPage() {
  const { state } = useUnified();
  const ws = state.workspaceId;
  const { data } = usePoll(
    () => (ws ? api.listConnections(ws) : Promise.resolve({ items: [] })),
    4000,
    [ws],
  );
  const conns = data?.items ?? [];
  const done = (kind: string) =>
    conns.find((c) => c.kind === kind && c.state === "configured");
  const complete = STEPS.every((s) => done(s.kind));

  return (
    <>
      <div className="page-head">
        <h1>Setup checklist</h1>
        <p className="muted">
          Minimum for MVP: email/password (signed in) + OpenCode Zen + Modal +
          GitHub. No Codex/ChatGPT required.
        </p>
      </div>
      {complete && (
        <p className="notice" role="status">
          All three connections configured — you can compose a session.
        </p>
      )}
      <div className="int-grid">
        {STEPS.map((s) => (
          <StepCard key={s.kind} step={s} existing={done(s.kind)} />
        ))}
      </div>
    </>
  );
}
