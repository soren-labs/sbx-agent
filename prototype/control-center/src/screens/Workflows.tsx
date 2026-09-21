import React, { useContext, useState } from "react";
import { AppCtx } from "../App";
import { WORKFLOWS } from "../mock";
import { AgentBadge, Badge, Card, Empty, fmtAgo, KV, ProviderBadge, RunBadge } from "../ui";

export default function Workflows({ id }: { id?: string }) {
  const { scenario, nav } = useContext(AppCtx);
  const wfs = scenario === "coldstart" ? [] : WORKFLOWS;
  const wf = wfs.find((w) => w.workflow_id === id) ?? wfs[0];
  const [cleanupDone, setCleanupDone] = useState(false);

  if (!wf) {
    return (
      <div>
        <h1>Workflows &amp; recovery</h1>
        <div className="sub"><code>GET /v1/workflows/{`{id}`}</code> — recovery view keyed by (caller key, workflow_id)</div>
        <Empty title="No workflow bindings" hint="Bind agents at create time with metadata.{workflow_id, task_id, role}." />
      </div>
    );
  }

  const p = wf.progress;

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>Workflow <code>{wf.workflow_id}</code></h1>
          <div className="sub">
            Low-cost recovery read: bound agents, latest runs and progress — served from persisted
            records only, no sandbox I/O.
          </div>
        </div>
        <div className="row">
          {wfs.length > 1 && (
            <select value={wf.workflow_id} onChange={(e) => nav(`/workflows/${e.target.value}`)}>
              {wfs.map((w) => <option key={w.workflow_id}>{w.workflow_id}</option>)}
            </select>
          )}
          <button className="btn danger" onClick={() => setCleanupDone(true)}>Scoped cleanup — DELETE /v1/workflows/{wf.workflow_id}</button>
        </div>
      </div>

      {cleanupDone && (
        <div className="alert blue">
          <strong>Cleanup result (mock):</strong>{" "}
          <code>{"{ matched: 3, closed: [ag_7f3k2, ag_9x1mq], already_terminal: [], missing: [ag_dead0], skipped: [] }"}</code>
          {" "}— idempotent; agents owned by another key land in <code>skipped</code>, never closed.
        </div>
      )}

      <div className="grid cols-4 section">
        <Card title="Tasks"><div className="metric">{p.tasks}</div></Card>
        <Card title="Agents"><div className="metric">{p.agents} <small>({p.open_agents} open)</small></div></Card>
        <Card title="Runs"><div className="metric">{p.runs}</div></Card>
        <Card title="Latest runs">
          <div className="row">
            {Object.entries(p.latest_runs_by_status).map(([s, n]) => (
              <Badge key={s} tone={s === "FINISHED" ? "cyan" : s === "RUNNING" ? "green" : s === "ERROR" ? "red" : "gray"}>{s} ×{n}</Badge>
            ))}
          </div>
          <div className="small muted" style={{ marginTop: 8 }}>all_terminal: {String(p.all_terminal)}</div>
        </Card>
      </div>

      <Card title="Bound agents">
        <table>
          <thead>
            <tr><th>Agent</th><th>Task</th><th>Role</th><th>Status</th><th>Provider</th><th>Runs</th><th>Latest run</th><th>Attached</th><th></th></tr>
          </thead>
          <tbody>
            {wf.agents.map((a) => (
              <tr key={a.agent_id}>
                <td className="mono small">{a.agent_id}</td>
                <td className="mono small">{a.task_id}{a.parent_task_id && <div className="muted small">← {a.parent_task_id}</div>}</td>
                <td><Badge tone="blue">{a.role}</Badge></td>
                <td>{a.status === "missing" ? <Badge tone="red">missing</Badge> : <AgentBadge s={a.status as never} />}</td>
                <td>{a.provider ? <ProviderBadge p={a.provider} /> : "—"}</td>
                <td>{a.runs}</td>
                <td>{a.latest_run ? <RunBadge s={a.latest_run.status} /> : "—"}</td>
                <td className="small muted">{fmtAgo(a.attached_at)}</td>
                <td>{a.status !== "missing" && <button className="btn sm ghost" onClick={() => nav(`/agents/${a.agent_id}`)}>open →</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <div className="card mt">
        <h3>Recovery flow</h3>
        <div className="small muted">
          A fresh client process only needs its API key + <code>workflow_id</code>:{" "}
          <code>GET /v1/workflows/{`{id}`}</code> returns bound agents, latest runs and artifact refs
          to resume polling/streaming (SSE reconnects with <code>Last-Event-ID</code>).{" "}
          <code>status: missing</code> means the agent record is gone — durable truth is the run
          ledger, not the sandbox.
        </div>
      </div>
    </div>
  );
}
