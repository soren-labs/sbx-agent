import React, { useContext } from "react";
import { AppCtx } from "../App";
import { AGENTS, ACCOUNTS, RUNS, PROVIDERS } from "../mock";
import { AgentBadge, Badge, Card, Empty, fmtAgo, fmtUsd, fmtTok, RunBadge } from "../ui";

export default function Overview() {
  const { scenario, nav } = useContext(AppCtx);
  const agents = scenario === "coldstart" ? [] : AGENTS;
  const accounts = scenario === "coldstart" ? [] : ACCOUNTS;
  const degraded = scenario === "degraded";

  const live = agents.filter((a) => ["creating", "idle", "running"].includes(a.status));
  const running = agents.filter((a) => a.status === "running");
  const cap = 8;
  const badAccounts = accounts.filter((a) => a.status === "invalid" || a.status === "cooling");
  const allRuns = Object.values(RUNS).flat().sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const costToday = agents.reduce((s, a) => s + (a.cost_estimate_usd ?? 0), 0);

  return (
    <div>
      <h1>Overview</h1>
      <div className="sub">
        Fleet readiness, live agents and recent runs — everything on this screen is mock data shaped
        on <code>/v1</code>.
      </div>

      {degraded && (
        <div className="alert amber">
          <strong>Fleet degraded:</strong> 1 account <code>auth_invalid</code>, 2 accounts in
          cooldown (<code>rate_limited</code>). Provider <code>antigravity</code> has no schedulable
          capacity.
        </div>
      )}
      {scenario === "coldstart" && (
        <div className="alert blue">
          <strong>Fresh deployment.</strong> No accounts imported yet — import provider credentials
          to make providers schedulable: <code>POST /v1/accounts</code> (admin scope).
        </div>
      )}

      <div className="grid cols-4 section">
        <Card title="Control plane">
          <div className="metric" style={{ color: "var(--green)" }}>up</div>
          <div className="small muted mono">GET /v1/me → 200 · key_01 · scopes [agents, admin]</div>
        </Card>
        <Card title="Live agents">
          <div className="metric">
            {live.length} <small>/ cap {cap}</small>
          </div>
          <div className="progressbar mt" style={{ marginTop: 8 }}>
            <div style={{ width: `${(live.length / cap) * 100}%` }} />
          </div>
          <div className="small muted" style={{ marginTop: 6 }}>
            {running.length} running · reaper retention 5m · hard timeout 4h
          </div>
        </Card>
        <Card title="Accounts ready">
          <div className="metric" style={{ color: badAccounts.length ? "var(--amber)" : "var(--green)" }}>
            {accounts.filter((a) => a.status === "active").length} <small>/ {accounts.length}</small>
          </div>
          <div className="small muted">{badAccounts.length} need attention</div>
        </Card>
        <Card title="Cost (all agents)">
          <div className="metric">{fmtUsd(costToday)}</div>
          <div className="small muted">estimate · sandbox seconds metered separately</div>
        </Card>
      </div>

      <div className="grid cols-2 section">
        <Card
          title="Provider readiness"
          actions={<button className="btn sm" onClick={() => nav("/providers")}>Manage →</button>}
        >
          <table>
            <thead>
              <tr><th>Provider</th><th>Status</th><th>Free accts</th><th>CLI</th></tr>
            </thead>
            <tbody>
              {PROVIDERS.map((p) => {
                const accts = accounts.filter((a) => a.provider === p.id);
                const free = accts.filter((a) => a.status === "active" && a.running < a.max_concurrent).length;
                return (
                  <tr key={p.id}>
                    <td><span className="mono">{p.id}</span></td>
                    <td>
                      <Badge tone={p.status === "Stable" ? "green" : "amber"}>{p.status}</Badge>
                      {scenario !== "coldstart" && free === 0 && (
                        <Badge tone="red">unready</Badge>
                      )}
                    </td>
                    <td>{scenario === "coldstart" ? "0 — none imported" : `${free} / ${accts.length}`}</td>
                    <td className="small muted">{p.cli}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>

        <Card title="Active agents">
          {live.length === 0 ? (
            <Empty
              title="No live agents"
              hint="Create your first agent — one POST /v1/agents spins up an isolated sandbox and queues run 1."
              action={<button className="btn primary" onClick={() => nav("/agents/create")}>Create agent</button>}
            />
          ) : (
            <table>
              <tbody>
                {live.map((a) => (
                  <tr key={a.id} className="clickable" onClick={() => nav(`/agents/${a.id}`)}>
                    <td className="mono small">{a.id}</td>
                    <td>{a.name}</td>
                    <td><span className="mono small muted">{a.provider}/{a.model}</span></td>
                    <td><AgentBadge s={a.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="Recent runs">
        {allRuns.length === 0 ? (
          <Empty title="No runs yet" hint="Run terminal states persist in the run ledger — they survive sandbox teardown." />
        ) : (
          <table>
            <thead>
              <tr><th>Run</th><th>Agent</th><th>Status</th><th>Error</th><th>Tokens</th><th>Updated</th></tr>
            </thead>
            <tbody>
              {allRuns.slice(0, 6).map((r) => (
                <tr key={r.id} className="clickable" onClick={() => nav(`/agents/${r.agent_id}`)}>
                  <td className="mono">{r.id}</td>
                  <td className="mono small">{r.agent_id}</td>
                  <td><RunBadge s={r.status} /></td>
                  <td className="small">{r.error ? <code>{r.error.code}</code> : "—"}</td>
                  <td className="small muted">{r.usage ? `${fmtTok(r.usage.input_tokens)} in / ${fmtTok(r.usage.output_tokens)} out` : "—"}</td>
                  <td className="small muted">{fmtAgo(r.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
