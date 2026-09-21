import React, { useContext, useState } from "react";
import { AppCtx } from "../App";
import { AGENTS, WORKSPACES, ARTIFACTS } from "../mock";
import { AgentBadge, Badge, Card, Empty, ErrorCard, Field, fmtTs, KV, shortSha } from "../ui";

export default function Workspace({ id }: { id?: string }) {
  const { scenario, nav } = useContext(AppCtx);
  const agentId = id ?? AGENTS[0].id;
  const agent = AGENTS.find((a) => a.id === agentId);
  const ws = WORKSPACES[agentId];
  const [reviewSha, setReviewSha] = useState("");
  const [comment, setComment] = useState("");
  const [handoffKind, setHandoffKind] = useState<"artifact_id" | "head_sha" | "pull_request">("artifact_id");
  const [handoffVal, setHandoffVal] = useState("");
  const [handoffSha, setHandoffSha] = useState("");
  const [simError, setSimError] = useState<string | null>(null);

  const withWorkspace = AGENTS.filter((a) => WORKSPACES[a.id]);

  if (scenario === "coldstart" || !agent) {
    return (
      <div>
        <h1>Workspace &amp; review</h1>
        <Empty title="No workspaces yet" hint="Agents created with a workspace declaration get a durable WorkspaceRecord." />
      </div>
    );
  }

  const errors: Record<string, { code: string; message: string }> = {
    head_sha_mismatch: { code: "head_sha_mismatch", message: "Review head disagrees with recorded workspace head — explicit 409, never a silent mislabel." },
    turn_in_progress: { code: "turn_in_progress", message: "Agent must be idle with a live sandbox — a running turn is 409." },
    base_sha_mismatch: { code: "base_sha_mismatch", message: "Handoff artifact base differs from workspace head — base gap fails closed." },
  };

  return (
    <div>
      <h1>Workspace, git &amp; handoff</h1>
      <div className="sub">
        Durable record (<code>GET /v1/agents/{`{id}`}/workspace</code>), review pinning, publish and
        cross-agent handoff — <code>docs/repo-workflow.md</code>.
      </div>

      <div className="row" style={{ marginBottom: 16 }}>
        <span className="small muted">Agent:</span>
        <select
          value={agentId}
          onChange={(e) => nav(`/workspace/${e.target.value}`)}
          style={{ background: "#0e121c", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 7, padding: "6px 8px", fontSize: 13 }}
        >
          {withWorkspace.map((a) => <option key={a.id} value={a.id}>{a.id} — {a.name}</option>)}
          <option value="ag_2p8wr">ag_2p8wr — no workspace (404)</option>
        </select>
        <AgentBadge s={agent.status} />
      </div>

      {!ws ? (
        <ErrorCard http={404} body={{ error: { code: "workspace_not_found", message: `agent ${agentId} declared no workspace` } }} />
      ) : (
        <div className="grid cols-2">
          <div className="grid">
            <Card title="WorkspaceRecord">
              <KV rows={[
                ["repo", <code>{ws.repo}</code>],
                ["base_ref", <code>{ws.base_ref}</code>],
                ["base_sha", <code className="mono">{shortSha(ws.base_sha)}</code>],
                ["checkout_sha", <code>{shortSha(ws.checkout_sha)}</code>],
                ["head_sha", <code>{shortSha(ws.head_sha)}</code>],
                ["reviewed_head_sha", ws.reviewed_head_sha ? <Badge tone="green"><code>{shortSha(ws.reviewed_head_sha)}</code></Badge> : <span className="muted">not pinned</span>],
                ["branch", ws.branch ? <code>{ws.branch}</code> : <span className="muted">—</span>],
                ["pushed_head_sha", ws.pushed_head_sha ? <code>{shortSha(ws.pushed_head_sha)}</code> : <span className="muted">—</span>],
                ["updated", fmtTs(ws.updated_at)],
              ]} />
            </Card>

            <Card title="Pull request (recorded by publish)">
              {ws.pull_request ? (
                <KV rows={[
                  ["PR", <a href="#">{ws.pull_request.url}</a>],
                  ["state / draft", <span>{ws.pull_request.state}{ws.pull_request.draft ? " · draft" : ""}</span>],
                  ["ref", <code>{ws.pull_request.ref}</code>],
                  ["pinned head", <code>{shortSha(ws.pull_request.head_sha)}</code>],
                  ["base", <code>{ws.pull_request.base}</code>],
                ]} />
              ) : (
                <Empty title="No PR recorded" hint="git.auto_create_pr opens one via the opt-in GitHub bridge after publish." />
              )}
              {ws.git && (
                <div className="mt">
                  <div className="small muted" style={{ marginBottom: 6 }}>Declared git policy:</div>
                  <code className="small">{JSON.stringify(ws.git)}</code>
                  <div className="row mt">
                    <button className="btn sm" disabled={!ws.git.push} title={ws.git.push ? "POST /v1/agents/{id}/git/publish" : "git.push not declared"}>
                      Publish work branch →
                    </button>
                    <span className="small muted">verifies ls-remote resolved to exactly the pushed head — drift fails closed</span>
                  </div>
                </div>
              )}
            </Card>
          </div>

          <div className="grid">
            <Card title="Pin reviewed head — POST …/workspace/review">
              <Field label="head_sha" hint="Omit to pin the recorded head; a disagreeing value → 409 head_sha_mismatch">
                <input className="mono" value={reviewSha} onChange={(e) => setReviewSha(e.target.value)} placeholder={shortSha(ws.head_sha)} />
              </Field>
              <Field label="Review comment (optional)" hint="Posts a machine-readable PR comment — never a formal GitHub approval (shared identity)">
                <textarea value={comment} onChange={(e) => setComment(e.target.value)} placeholder='{"verdict":"pass","notes":"…"}' style={{ minHeight: 60 }} />
              </Field>
              <div className="row">
                <button className="btn primary sm" onClick={() => setSimError(null)}>Pin reviewed head</button>
                <button className="btn sm ghost" onClick={() => setSimError("head_sha_mismatch")}>simulate mismatch</button>
              </div>
            </Card>

            <Card title="Apply handoff — POST …/handoff">
              <div className="radio-row" style={{ marginBottom: 12 }}>
                {(["artifact_id", "head_sha", "pull_request"] as const).map((k) => (
                  <div key={k} className={`radio-card ${handoffKind === k ? "sel" : ""}`} onClick={() => setHandoffKind(k)}>
                    <div className="t mono small">{k}</div>
                  </div>
                ))}
              </div>
              {handoffKind === "artifact_id" && (
                <Field label="Durable artifact" hint="Validates repo identity, payload sha256, and that the workspace stands exactly on the artifact's base_sha">
                  <select value={handoffVal} onChange={(e) => setHandoffVal(e.target.value)}>
                    <option value="">select…</option>
                    {ARTIFACTS.map((a) => <option key={a.artifact_id} value={a.artifact_id}>{a.artifact_id} (base {shortSha(a.base_sha)})</option>)}
                  </select>
                </Field>
              )}
              {handoffKind === "head_sha" && (
                <Field label="head_sha" hint="Exact commit; must descend from declared base">
                  <input className="mono" value={handoffSha} onChange={(e) => setHandoffSha(e.target.value)} />
                </Field>
              )}
              {handoffKind === "pull_request" && (
                <div className="grid cols-2">
                  <Field label="ref"><input className="mono" value={handoffVal} onChange={(e) => setHandoffVal(e.target.value)} placeholder="refs/pull/42/head" /></Field>
                  <Field label="head_sha"><input className="mono" value={handoffSha} onChange={(e) => setHandoffSha(e.target.value)} placeholder="pinned head" /></Field>
                </div>
              )}
              <div className="row">
                <button className="btn primary sm" onClick={() => setSimError(null)}>Apply handoff</button>
                <button className="btn sm ghost" onClick={() => setSimError("base_sha_mismatch")}>simulate base gap</button>
                <button className="btn sm ghost" onClick={() => setSimError("turn_in_progress")}>simulate busy</button>
              </div>
              {simError && <div className="mt"><ErrorCard http={409} body={{ error: errors[simError] }} /></div>}
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
