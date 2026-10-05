/**
 * Session detail — Conversation (messages + live event tail), Activity,
 * Changes (live observe + sealed ChangeSets + Delivery), Files (read-only),
 * Child Sessions (delegations). All state comes from API rows; the UI never
 * decides terminal success.
 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type ChangeSet,
  type Delegation,
  type Delivery,
} from "../api/unified";
import { useEventTail, usePoll, useUnified } from "../state/unified";

const TABS = ["conversation", "activity", "changes", "files", "children"] as const;
type Tab = (typeof TABS)[number];

function ConversationTab({ sessionId }: { sessionId: string }) {
  const { data: msgs, refresh } = usePoll(
    () => api.listMessages(sessionId),
    3000,
    [sessionId],
  );
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.postMessage(sessionId, { text });
      setText("");
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.error.message : "failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <div className="convo">
        {(msgs?.items ?? []).map((m) => (
          <div className="msg" key={m.id} data-role={m.role}>
            <div className="faint small">
              #{m.ordinal} {m.role} · {m.routing}
              {m.routed_turn_id ? ` → turn ${m.routed_turn_id.slice(0, 12)}` : ""}
            </div>
            <p>{m.content.text ?? JSON.stringify(m.content)}</p>
          </div>
        ))}
        {(msgs?.items ?? []).length === 0 && (
          <p className="empty">No messages yet.</p>
        )}
      </div>
      <form className="followup-bar" onSubmit={send}>
        <input
          aria-label="Send a message"
          placeholder="Follow up — queues a new turn"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="btn btn-primary btn-sm" disabled={busy} type="submit">
          Send
        </button>
      </form>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function ActivityTab({ sessionId }: { sessionId: string }) {
  const { items, watermark } = useEventTail(sessionId, 1500);
  return (
    <div>
      <p className="faint small">event watermark: {watermark}</p>
      {items.length === 0 ? (
        <p className="empty">Waiting for committed events…</p>
      ) : (
        <ul className="session-list">
          {items.map((e) => (
            <li key={e.seq} className="session-meta-row card">
              <span className="mono small">#{e.seq}</span>
              <span className="grow">{e.type}</span>
              <span className="faint small">{e.recorded_at}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ChangesTab({ sessionId }: { sessionId: string }) {
  const { data: sets, refresh } = usePoll(
    () => api.listChangeSets(sessionId),
    5000,
    [sessionId],
  );
  const [deliveries, setDeliveries] = useState<Record<string, Delivery>>({});
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const capture = async () => {
    setError(null);
    setNote(null);
    try {
      const r = await api.capture(sessionId, { origin: "explicit" });
      setNote(
        r.changeset_id
          ? `captured ${r.changeset_id}`
          : `capture job ${r.job_id ?? "queued"}`,
      );
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.error.message : "failed");
    }
  };

  const deliver = async (cs: ChangeSet, transport: string) => {
    setError(null);
    try {
      const r = await api.createDelivery(cs.id, {
        transport,
        target: { repository: cs.repository ?? "" },
      });
      setDeliveries((d) => ({ ...d, [cs.id]: r.delivery }));
    } catch (e) {
      setError(e instanceof ApiError ? `${e.error.code}: ${e.error.message}` : "failed");
    }
  };

  return (
    <div>
      <div className="section-head">
        <h3>ChangeSets</h3>
        <button className="btn btn-sm" onClick={() => void capture()}>
          Capture now
        </button>
      </div>
      {note && <p className="faint small">{note}</p>}
      {(sets?.items ?? []).length === 0 && (
        <p className="empty">No sealed ChangeSets.</p>
      )}
      {(sets?.items ?? []).map((cs) => (
        <div className="card deliver-card" key={cs.id}>
          <div className="session-meta-row">
            <span className="mono small">{cs.id}</span>
            <span className="faint small">{cs.capture_origin}</span>
          </div>
          <div className="kv small">
            <span>subject</span>
            <span className="mono">{cs.subject_digest.slice(0, 24)}…</span>
            <span>head</span>
            <span className="mono">{cs.head_sha ?? "—"}</span>
          </div>
          <div className="inline-act">
            <button
              className="btn btn-sm"
              onClick={() => void deliver(cs, "git_branch")}
            >
              Deliver: branch
            </button>
            <button
              className="btn btn-sm"
              onClick={() => void deliver(cs, "pull_request")}
            >
              Deliver: PR
            </button>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => void api.applyChangeSet(cs.id, sessionId)}
            >
              Re-apply here
            </button>
          </div>
          {deliveries[cs.id] && (
            <p className="small">
              delivery {deliveries[cs.id].id} —{" "}
              <strong>{deliveries[cs.id].state}</strong>
            </p>
          )}
        </div>
      ))}
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function FilesTab({ sessionId }: { sessionId: string }) {
  const [path, setPath] = useState(".");
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { data, error: listErr } = usePoll(
    () => api.listFiles(sessionId, path),
    30000,
    [sessionId, path],
  );

  const open = async (p: string) => {
    setError(null);
    try {
      const r = await api.readFile(sessionId, p);
      setContent(atob(r.content_b64));
    } catch (e) {
      setContent(null);
      setError(e instanceof ApiError ? e.error.message : "unreadable");
    }
  };

  return (
    <div>
      <label className="field">
        <span>Path</span>
        <input value={path} onChange={(e) => setPath(e.target.value)} />
      </label>
      {listErr && (
        <p className="notice">
          {listErr instanceof ApiError
            ? `${listErr.error.code}: ${listErr.error.message}`
            : "executor unavailable"}
        </p>
      )}
      <ul className="session-list">
        {(data?.entries ?? []).map((f) => (
          <li key={f.path} className="file-row">
            <button className="btn-ghost mono small" onClick={() => void open(f.path)}>
              {f.path}
            </button>
            <span className="faint small">{f.kind}</span>
          </li>
        ))}
      </ul>
      {error && <p className="notice">{error}</p>}
      {content !== null && <pre className="diff">{content}</pre>}
    </div>
  );
}

function ChildrenTab({ sessionId }: { sessionId: string }) {
  const { data, refresh } = usePoll(
    () => api.listDelegations(sessionId),
    5000,
    [sessionId],
  );
  const [role, setRole] = useState("reviewer");
  const [prompt, setPrompt] = useState("");
  const [results, setResults] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  const spawn = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await api.spawnDelegation(sessionId, {
        role,
        prompt,
        result_contract: { kind: "GenericResult" },
      });
      setPrompt("");
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? `${err.error.code}: ${err.error.message}` : "failed");
    }
  };

  const showResult = async (d: Delegation) => {
    try {
      const r = await api.getDelegation(d.id);
      setResults((m) => ({
        ...m,
        [d.id]: r.result
          ? `${r.result.validation_status}: ${JSON.stringify(r.result.value).slice(0, 300)}`
          : "no published result yet",
      }));
    } catch (e) {
      setResults((m) => ({ ...m, [d.id]: "unavailable" }));
    }
  };

  return (
    <div>
      <form className="card" onSubmit={spawn}>
        <div className="section-head">
          <h3>Delegate to a child session</h3>
        </div>
        <label className="field">
          <span>Role</span>
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="reviewer">reviewer</option>
            <option value="tester">tester</option>
            <option value="researcher">researcher</option>
            <option value="integrator">integrator</option>
          </select>
        </label>
        <label className="field">
          <span>Prompt</span>
          <textarea
            rows={3}
            required
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </label>
        <button className="btn btn-primary btn-sm" type="submit">
          Spawn
        </button>
      </form>
      {(data?.items ?? []).map((d) => (
        <div className="card" key={d.id}>
          <div className="session-meta-row">
            <strong>{d.role}</strong>
            <span className={`pill pill-${d.state}`}>{d.state}</span>
            <Link className="small" to={`/sessions/${d.child_session_id}`}>
              child →
            </Link>
          </div>
          <div className="inline-act">
            <button className="btn btn-sm btn-ghost" onClick={() => void showResult(d)}>
              Result
            </button>
            {d.state === "active" && (
              <button
                className="btn btn-sm btn-danger"
                onClick={() => void api.cancelDelegation(d.id).then(refresh)}
              >
                Cancel
              </button>
            )}
          </div>
          {results[d.id] && <pre className="diff small">{results[d.id]}</pre>}
        </div>
      ))}
      {error && <p className="notice">{error}</p>}
    </div>
  );
}

export function SessionDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { state } = useUnified();
  const [tab, setTab] = useState<Tab>("conversation");
  const { data, error, refresh } = usePoll(
    () => (id ? api.getSession(id) : Promise.reject("no id")),
    4000,
    [id],
  );

  if (error) {
    return <p className="notice">{(error as ApiError).error?.message ?? "failed"}</p>;
  }
  if (!data) return <p className="empty">Loading…</p>;
  const s = data.session;
  const turn = data.active_turn;

  return (
    <>
      <div className="page-head session-head">
        <div>
          <h1>{s.title || s.id}</h1>
          <p className="muted small">
            <span className="mono">{s.harness.provider_id}:{s.harness.model}</span>{" "}
            · {String(s.projectless_spec?.repository ?? "projectless")} · gen{" "}
            {data.worktree?.generation ?? "?"}
          </p>
        </div>
        <div className="session-actions">
          <span className={`pill pill-${s.lifecycle}`}>{s.lifecycle}</span>
          {turn && <span className={`pill pill-${turn.state}`}>turn {turn.state}</span>}
          {data.executor && (
            <span className="pill pill-running">
              {data.executor.backend} lease
            </span>
          )}
          {s.lifecycle === "open" && (
            <button
              className="btn btn-sm btn-danger"
              onClick={() => void api.closeSession(s.id).then(refresh)}
            >
              Close
            </button>
          )}
        </div>
      </div>
      <div className="tabs" role="tablist" aria-label="Session surfaces">
        {TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            className={tab === t ? "active" : ""}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === "conversation" && <ConversationTab sessionId={s.id} />}
      {tab === "activity" && <ActivityTab sessionId={s.id} />}
      {tab === "changes" && <ChangesTab sessionId={s.id} />}
      {tab === "files" && <FilesTab sessionId={s.id} />}
      {tab === "children" && <ChildrenTab sessionId={s.id} />}
      <p className="faint small">{state.user?.email}</p>
    </>
  );
}
