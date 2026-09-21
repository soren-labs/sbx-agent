import React, { useContext, useState } from "react";
import { AppCtx } from "../App";
import { AGENTS, type AgentStatus, type ProviderId } from "../mock";
import { AgentBadge, Badge, Empty, fmtAgo, fmtUsd, fmtTok } from "../ui";

const PROVIDER_FILTERS = ["all", "codex", "devin", "antigravity", "grok", "opencode"];
const STATUS_FILTERS = ["all", "creating", "idle", "running", "closed", "timed_out", "lost"];

export default function Agents() {
  const { scenario, nav } = useContext(AppCtx);
  const [prov, setProv] = useState("all");
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");

  let agents = scenario === "coldstart" ? [] : AGENTS;
  if (scenario === "empty") agents = agents.filter((a) => a.status !== "running");
  const filtered = agents.filter(
    (a) =>
      (prov === "all" || a.provider === prov) &&
      (status === "all" || a.status === status) &&
      (!q || a.name.toLowerCase().includes(q.toLowerCase()) || a.id.includes(q))
  );

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>Agents</h1>
          <div className="sub">
            <code>GET /v1/agents</code> — one agent = one Modal Sandbox.
          </div>
        </div>
        <button className="btn primary" onClick={() => nav("/agents/create")}>+ Create agent</button>
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        <input
          placeholder="Filter by name or id…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ background: "#0e121c", border: "1px solid var(--border)", borderRadius: 7, color: "var(--text)", padding: "7px 10px", fontSize: 13, width: 220 }}
        />
        <div className="pill-toggle">
          {PROVIDER_FILTERS.map((p) => (
            <button key={p} className={prov === p ? "on" : ""} onClick={() => setProv(p)}>{p}</button>
          ))}
        </div>
        <div className="pill-toggle">
          {STATUS_FILTERS.map((s) => (
            <button key={s} className={status === s ? "on" : ""} onClick={() => setStatus(s)}>{s}</button>
          ))}
        </div>
      </div>

      {filtered.length === 0 ? (
        <Empty
          title={agents.length === 0 ? "No agents yet" : "No agents match these filters"}
          hint={agents.length === 0 ? "POST /v1/agents creates an agent and immediately queues its first run." : "Widen the filters or clear the search."}
          action={agents.length === 0 ? <button className="btn primary" onClick={() => nav("/agents/create")}>Create agent</button> : undefined}
        />
      ) : (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th>ID</th><th>Name</th><th>Provider / model</th><th>Account</th><th>Status</th>
                <th>Workflow</th><th>Tokens (in/out)</th><th>Cost</th><th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((a) => (
                <tr key={a.id} className="clickable" onClick={() => nav(`/agents/${a.id}`)}>
                  <td className="mono small">{a.id}</td>
                  <td>{a.name}</td>
                  <td><span className="mono small">{a.provider}</span> <span className="muted small">· {a.model}</span></td>
                  <td className="mono small muted">{a.account_id}</td>
                  <td><AgentBadge s={a.status} /></td>
                  <td className="small">{a.metadata ? <Badge tone="blue">{a.metadata.workflow_id}</Badge> : "—"}</td>
                  <td className="small muted">{a.usage ? `${fmtTok(a.usage.input_tokens)} / ${fmtTok(a.usage.output_tokens)}` : "unmeasured"}</td>
                  <td className="small">{fmtUsd(a.cost_estimate_usd)}</td>
                  <td className="small muted">{fmtAgo(a.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="small muted" style={{ marginTop: 10 }}>
        Filters map to <code>?provider=</code>, <code>?status=</code>, <code>?account_id=</code>,{" "}
        <code>?workflow_id=</code> query params; pagination via <code>?cursor=</code> →{" "}
        <code>next_cursor</code>.
      </div>
    </div>
  );
}
