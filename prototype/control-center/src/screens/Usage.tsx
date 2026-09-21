import React, { useContext } from "react";
import { AppCtx } from "../App";
import { AGENTS } from "../mock";
import { AgentBadge, Badge, Card, Empty, fmtAgo, fmtTok, fmtUsd } from "../ui";

export default function Usage() {
  const { scenario } = useContext(AppCtx);
  const agents = scenario === "coldstart" ? [] : AGENTS;
  const totalCost = agents.reduce((s, a) => s + (a.cost_estimate_usd ?? 0), 0);
  const totalIn = agents.reduce((s, a) => s + (a.usage?.input_tokens ?? 0), 0);
  const totalCached = agents.reduce((s, a) => s + (a.usage?.cached_input_tokens ?? 0), 0);
  const totalOut = agents.reduce((s, a) => s + (a.usage?.output_tokens ?? 0), 0);
  const sandboxSec = agents.length * 3120; // mock

  return (
    <div>
      <h1>Usage, cost &amp; lifecycle</h1>
      <div className="sub">
        Per-agent <code>GET /v1/agents/{`{id}`}/usage</code> — <code>usage</code>,{" "}
        <code>cost_estimate_usd</code>, <code>sandbox_seconds</code>. Unmeasured usage is{" "}
        <code>null</code>, never fabricated zeros.
      </div>

      <div className="grid cols-4 section">
        <Card title="Input tokens"><div className="metric">{fmtTok(totalIn)}</div>
          <div className="small muted">{fmtTok(totalCached)} cached ({totalIn ? Math.round((totalCached / totalIn) * 100) : 0}%)</div></Card>
        <Card title="Output tokens"><div className="metric">{fmtTok(totalOut)}</div></Card>
        <Card title="Sandbox time"><div className="metric">{(sandboxSec / 3600).toFixed(1)}h</div>
          <div className="small muted">billed by Modal, metered per sandbox</div></Card>
        <Card title="Cost estimate"><div className="metric">{fmtUsd(totalCost)}</div>
          <div className="small muted">subscription CLI usage — no per-token resale</div></Card>
      </div>

      <Card title="Per-agent usage">
        {agents.length === 0 ? (
          <Empty title="Nothing metered yet" hint="Usage attaches to runs as they measure token counts." />
        ) : (
          <table>
            <thead>
              <tr><th>Agent</th><th>Provider/model</th><th>Status</th><th>Input</th><th>Cached</th><th>Output</th><th>Reasoning</th><th>Cost est.</th><th>Updated</th></tr>
            </thead>
            <tbody>
              {agents.map((a) => (
                <tr key={a.id}>
                  <td className="mono small">{a.id}</td>
                  <td className="mono small">{a.provider}/{a.model}</td>
                  <td><AgentBadge s={a.status} /></td>
                  <td className="small">{a.usage ? fmtTok(a.usage.input_tokens) : <Badge tone="gray">unmeasured</Badge>}</td>
                  <td className="small muted">{fmtTok(a.usage?.cached_input_tokens)}</td>
                  <td className="small">{fmtTok(a.usage?.output_tokens)}</td>
                  <td className="small muted">{fmtTok(a.usage?.reasoning_output_tokens)}</td>
                  <td className="small">{fmtUsd(a.cost_estimate_usd)}</td>
                  <td className="small muted">{fmtAgo(a.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <div className="grid cols-2 mt">
        <Card title="Agent lifecycle">
          <div className="row" style={{ flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
            {["creating", "idle", "running", "closed", "timed_out", "lost"].map((s) => (
              <Badge key={s} tone={{ creating: "blue", idle: "cyan", running: "green", closed: "gray", timed_out: "amber", lost: "red" }[s]}>{s}</Badge>
            ))}
          </div>
          <div className="small muted">
            <code>creating → running ⇄ idle → closed</code> via DELETE. The reaper reclaims an idle
            agent after the post-session retention (5m default); the sandbox's own{" "}
            <code>idle_timeout</code> bounds it independently; a hard <code>timeout</code> (4h
            default) is the backstop → <code>timed_out</code>. An agent whose sandbox vanished without
            a terminal record → <code>lost</code>. Terminal agents keep read-only history.
          </div>
        </Card>
        <Card title="Capacity &amp; throttles">
          <div className="small muted">
            <Badge tone="amber">429 concurrency_limit</Badge> global live-agent cap
            (<code>SBX_MAX_CONCURRENT</code>, per-key 2 / global 8) — idle agents hold slots until
            closed. <Badge tone="amber">429 provider_exhausted</Badge> when the auto scheduler finds
            no free account. Honor <code>error.retry_after</code>; close idle agents or run scoped
            cleanup to free slots.
          </div>
        </Card>
      </div>
    </div>
  );
}
