import React, { useContext, useState } from "react";
import { AppCtx } from "../App";
import { AGENTS, RUNS, TIMELINE, WORKSPACES, ARTIFACTS, type TimelineEvent } from "../mock";
import {
  AgentBadge, Badge, Card, Empty, ErrorCard, fmtAgo, fmtTs, fmtTok, fmtUsd,
  JsonView, KV, RunBadge, shortSha, Tabs,
} from "../ui";

const TONE_FOR_ITEM: Record<string, string> = {
  agent_message: "ok", command_execution: "run", file_change: "info", reasoning: "", error: "err",
};

function Timeline({ events }: { events: TimelineEvent[] }) {
  if (!events.length) return <Empty title="No events" hint="SSE frames arrive as the run executes; finished runs keep their events.jsonl record." />;
  return (
    <div className="timeline">
      {events.map((e) => (
        <div key={e.id} className={`tl-item ${e.item_type ? TONE_FOR_ITEM[e.item_type] ?? "" : e.type.includes("completed") || e.type.includes("finished") ? "ok" : "run"}`}>
          <div className="tl-head">
            <span className="mono small muted">id:{e.id}</span>
            <Badge tone="gray">{e.type}</Badge>
            {e.item_type && <Badge tone="blue">{e.item_type}</Badge>}
            <strong className="small">{e.title}</strong>
            {e.exit_code != null && <Badge tone={e.exit_code === 0 ? "green" : "red"}>exit {e.exit_code}</Badge>}
            <span className="small muted">{fmtAgo(e.at)}</span>
          </div>
          {e.detail && <div className="tl-detail">{e.detail}</div>}
        </div>
      ))}
    </div>
  );
}

export default function AgentDetail({ id }: { id: string }) {
  const { scenario, nav } = useContext(AppCtx);
  const agent = AGENTS.find((a) => a.id === id);
  const [tab, setTab] = useState("Runs");
  const [selRun, setSelRun] = useState<string | null>(null);

  if (!agent || scenario === "coldstart") {
    return (
      <div>
        <h1>Agent</h1>
        <div className="sub"><code>GET /v1/agents/{id}</code></div>
        <ErrorCard http={404} body={{ error: { code: "not_found", message: `agent ${id} not found (or owned by another key)` } }} />
        <button className="btn" onClick={() => nav("/agents")}>← Back to agents</button>
      </div>
    );
  }

  const runs = RUNS[agent.id] ?? [];
  const ws = WORKSPACES[agent.id];
  const arts = ARTIFACTS.filter((a) => a.producer.agent_id === agent.id);
  const cur = runs.find((r) => r.id === selRun) ?? runs[runs.length - 1];
  const tl = cur ? TIMELINE[`${agent.id}/${cur.id}`] ?? [] : [];
  const isLive = ["creating", "idle", "running"].includes(agent.status);
  const running = cur?.status === "RUNNING" || cur?.status === "CREATING";

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>{agent.name}</h1>
          <div className="sub mono">
            {agent.id} · {agent.provider} · {agent.model} · acct {agent.account_id}
          </div>
        </div>
        <div className="row">
          <AgentBadge s={agent.status} />
          {isLive && <button className="btn">New run (follow-up)</button>}
          {running && <button className="btn">Cancel run</button>}
          {isLive ? (
            <button className="btn danger">Close agent</button>
          ) : (
            <Badge tone="gray">terminal — history is read-only</Badge>
          )}
        </div>
      </div>

      <div className="grid cols-4 section">
        <Card title="Status"><div className="metric" style={{ fontSize: 18 }}><AgentBadge s={agent.status} /></div>
          <div className="small muted" style={{ marginTop: 6 }}>created {fmtAgo(agent.created_at)}</div></Card>
        <Card title="Tokens">
          {agent.usage ? (
            <div className="metric" style={{ fontSize: 18 }}>{fmtTok(agent.usage.input_tokens)} <small>in</small> · {fmtTok(agent.usage.output_tokens)} <small>out</small></div>
          ) : (
            <div className="metric" style={{ fontSize: 15, color: "var(--muted)" }}>unmeasured</div>
          )}
          {agent.usage?.cached_input_tokens != null && <div className="small muted">{fmtTok(agent.usage.cached_input_tokens)} cached</div>}
        </Card>
        <Card title="Cost estimate"><div className="metric" style={{ fontSize: 18 }}>{fmtUsd(agent.cost_estimate_usd)}</div>
          <div className="small muted"><code>GET /v1/agents/{agent.id}/usage</code></div></Card>
        <Card title="Workflow binding">
          {agent.metadata ? (
            <KV rows={[["workflow", <code>{agent.metadata.workflow_id}</code>], ["task", agent.metadata.task_id], ["role", agent.metadata.role]]} />
          ) : (
            <span className="muted small">none</span>
          )}
        </Card>
      </div>

      <Tabs tabs={["Runs", "Workspace", "Artifacts", "Raw JSON"]} cur={tab} onSel={setTab} />

      {tab === "Runs" && (
        <div className="grid" style={{ gridTemplateColumns: "300px 1fr" }}>
          <div className="grid">
            {runs.map((r) => (
              <div
                key={r.id}
                className={`card ${cur?.id === r.id ? "" : ""}`}
                style={{ cursor: "pointer", borderColor: cur?.id === r.id ? "var(--accent)" : undefined }}
                onClick={() => setSelRun(r.id)}
              >
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="mono">{r.id}</span>
                  <RunBadge s={r.status} />
                </div>
                <div className="small muted" style={{ marginTop: 6 }}>
                  {fmtTs(r.created_at)} → {r.finished_at ? fmtTs(r.finished_at) : "running…"}
                </div>
                {r.error && <div className="small" style={{ color: "var(--red)", marginTop: 4 }}><code>{r.error.code}</code> ({r.error.source})</div>}
                {r.output_contract && <div style={{ marginTop: 6 }}><Badge tone={r.output_contract.status === "valid" ? "green" : r.output_contract.status === "pending" ? "blue" : r.output_contract.status === "invalid" ? "red" : "gray"}>contract: {r.output_contract.status}</Badge></div>}
              </div>
            ))}
            <button className="btn" disabled={!isLive} title={isLive ? "" : "agent is terminal"}>
              + Follow-up run — POST /v1/agents/{agent.id}/runs
            </button>
          </div>

          <div>
            {cur?.error && (
              <ErrorCard
                body={{
                  error: {
                    code: cur.error.code,
                    message: `${cur.error.message} · source=${cur.error.source} · retryable=${cur.error.retryable}`,
                    retry_after: cur.error.retry_after,
                  },
                }}
              />
            )}
            {cur?.structured_output != null && (
              <Card title="Structured output (output_contract verdict)">
                <JsonView value={cur.structured_output} />
              </Card>
            )}
            <Card title={<span style={{ textTransform: "none" }}>Run timeline — <code>{cur?.id}</code> · SSE /v1/agents/{agent.id}/runs/{cur?.id}/stream</span>}>
              <div className="small muted" style={{ marginBottom: 12 }}>
                Frames: <code>id: &lt;events.jsonl line&gt;</code> / <code>event: &lt;type&gt;</code> /{" "}
                <code>data: &lt;json&gt;</code>, 15s keepalive, <code>Last-Event-ID</code> resume.
              </div>
              <Timeline events={tl} />
            </Card>
            {cur?.result?.text && (
              <div className="card" style={{ marginTop: 14 }}>
                <h3>Result</h3>
                <p className="small">{cur.result.text}</p>
                <div className="small muted">artifact_refs: {(cur.artifact_refs ?? []).map((x) => <code key={x} style={{ marginRight: 8 }}>{x}</code>)}</div>
              </div>
            )}
          </div>
        </div>
      )}

      {tab === "Workspace" && (
        ws ? (
          <Card title={<span style={{ textTransform: "none" }}>Workspace record — GET /v1/agents/{agent.id}/workspace</span>}>
            <KV
              rows={[
                ["repo", <code>{ws.repo}</code>],
                ["base_ref / base_sha", <span><code>{ws.base_ref}</code> @ <code>{shortSha(ws.base_sha)}</code></span>],
                ["checkout_sha", <code>{shortSha(ws.checkout_sha)}</code>],
                ["head_sha", <code>{shortSha(ws.head_sha)}</code>],
                ["reviewed_head_sha", ws.reviewed_head_sha ? <code>{shortSha(ws.reviewed_head_sha)}</code> : <span className="muted">not pinned</span>],
                ["git policy", ws.git ? <code>{JSON.stringify(ws.git)}</code> : <span className="muted">none</span>],
                ["pull_request", ws.pull_request ? <a href="#">{ws.pull_request.url}</a> : <span className="muted">none</span>],
              ]}
            />
            <div className="row mt">
              <button className="btn" onClick={() => nav(`/workspace/${agent.id}`)}>Open workspace console →</button>
            </div>
          </Card>
        ) : (
          <Empty title="No workspace declared" hint="This agent was created without a workspace declaration — GET /workspace returns 404 workspace_not_found." />
        )
      )}

      {tab === "Artifacts" && (
        arts.length ? (
          <div className="grid">
            {arts.map((a) => (
              <Card key={a.artifact_id} title={<code style={{ textTransform: "none" }}>{a.artifact_id}</code>}>
                <KV rows={[
                  ["format", a.format], ["base → head", <span className="mono">{shortSha(a.base_sha)} → {shortSha(a.head_sha)}</span>],
                  ["files", `${a.files.length} collected`], ["payloads", Object.keys(a.payloads).join(", ")],
                  ["created", fmtTs(a.created_at)],
                ]} />
                <div className="row mt"><button className="btn sm" onClick={() => nav(`/artifacts/${a.artifact_id}`)}>Inspect →</button></div>
              </Card>
            ))}
          </div>
        ) : (
          <Empty
            title="No artifacts yet"
            hint="POST /v1/agents/{id}/artifacts snapshots the workspace into a durable package while the sandbox is still alive."
            action={isLive ? <button className="btn">Snapshot artifact</button> : undefined}
          />
        )
      )}

      {tab === "Raw JSON" && <Card title={<span style={{ textTransform: "none" }}>GET /v1/agents/{agent.id}</span>}><JsonView value={agent} /></Card>}
    </div>
  );
}
