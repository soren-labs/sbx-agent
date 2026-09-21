import React, { useContext, useMemo, useState } from "react";
import { AppCtx } from "../App";
import { ACCOUNTS, MODELS, ARTIFACTS, type ProviderId } from "../mock";
import { Badge, Card, Field, JsonView } from "../ui";

const STEPS = ["Prompt", "Provider & account", "Workspace & git", "Contract & metadata", "Review"];

export default function CreateAgent() {
  const { scenario, nav } = useContext(AppCtx);
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [provider, setProvider] = useState<ProviderId>("codex");
  const [accountId, setAccountId] = useState("auto");
  const [model, setModel] = useState("");
  const [repo, setRepo] = useState("");
  const [baseRef, setBaseRef] = useState("main");
  const [baseSha, setBaseSha] = useState("");
  const [gitPolicy, setGitPolicy] = useState({ branch: "", push: false, auto_create_pr: false, target: "main", draft: true });
  const [handoffKind, setHandoffKind] = useState<"none" | "artifact_id" | "head_sha" | "pull_request">("none");
  const [handoffVal, setHandoffVal] = useState("");
  const [handoffSha, setHandoffSha] = useState("");
  const [schema, setSchema] = useState("");
  const [enforcement, setEnforcement] = useState<"strict" | "warn">("strict");
  const [wfId, setWfId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [role, setRole] = useState("worker");
  const [idleTimeout, setIdleTimeout] = useState("");

  const accounts = scenario === "coldstart" ? [] : ACCOUNTS;
  const provAccounts = accounts.filter((a) => a.provider === provider);
  const models = MODELS.filter((m) => m.provider === provider);
  const hasWorkspace = repo.trim() !== "";

  const problems: string[] = [];
  if (gitPolicy.auto_create_pr && !gitPolicy.push) problems.push("git.auto_create_pr requires git.push (400 workspace_invalid)");
  if (handoffKind !== "none" && !hasWorkspace) problems.push("handoff on create requires a workspace declaration (workspace_invalid)");
  if (provAccounts.length === 0) problems.push(`No ${provider} accounts imported — POST /v1/accounts first, or pick another provider`);
  const freeAcct = provAccounts.find((a) => a.status === "active" && a.running < a.max_concurrent);
  if (accountId === "auto" && provAccounts.length > 0 && !freeAcct) problems.push("All accounts busy/cooling — auto pick would 429 provider_exhausted");

  const payload = useMemo(() => {
    const body: Record<string, unknown> = {
      prompt: { text: prompt || "<prompt text>" },
      agent: { provider, account_id: accountId, ...(model ? { model } : {}) },
      ...(name ? { name } : {}),
      ...(idleTimeout ? { idle_timeout_s: Number(idleTimeout) } : {}),
    };
    if (hasWorkspace) {
      body.workspace = { repo, base_ref: baseRef, base_sha: baseSha || "<40-hex sha>" };
      if (gitPolicy.branch || gitPolicy.push) {
        body.git = { ...gitPolicy, branch: gitPolicy.branch || "sbx/<agent_id>" };
      }
    }
    if (handoffKind === "artifact_id") body.handoff = { artifact_id: handoffVal };
    if (handoffKind === "head_sha") body.handoff = { head_sha: handoffSha };
    if (handoffKind === "pull_request") body.handoff = { pull_request: { ref: handoffVal || "refs/pull/<n>/head", head_sha: handoffSha } };
    if (schema) body.output_contract = { schema: tryJson(schema), enforcement };
    if (wfId) body.metadata = { workflow_id: wfId, task_id: taskId || "<task>", role };
    return body;
  }, [prompt, provider, accountId, model, name, idleTimeout, hasWorkspace, repo, baseRef, baseSha, gitPolicy, handoffKind, handoffVal, handoffSha, schema, enforcement, wfId, taskId, role]);

  function tryJson(s: string) {
    try { return JSON.parse(s); } catch { return "<invalid JSON — 400 invalid_output_contract>"; }
  }

  return (
    <div>
      <h1>Create agent</h1>
      <div className="sub"><code>POST /v1/agents</code> — creates the sandbox and immediately queues run 1.</div>

      <div className="stepper">
        {STEPS.map((s, i) => (
          <div key={s} className={`step ${i < step ? "done" : ""} ${i === step ? "cur" : ""}`} onClick={() => setStep(i)} style={{ cursor: "pointer" }}>
            {i + 1}. {s}
          </div>
        ))}
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 380px" }}>
        <div className="card">
          {step === 0 && (
            <>
              <Field label="Name (optional)"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Fix flaky date test" /></Field>
              <Field label="Prompt" hint="The first run's instruction — prompt.text">
                <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Describe the task for run 1…" />
              </Field>
              <Field label="Idle timeout (s)" hint="Native sandbox idle bound; the post-session reaper retention (5m) applies after the last run">
                <input value={idleTimeout} onChange={(e) => setIdleTimeout(e.target.value.replace(/\D/g, ""))} placeholder="14400 default backstop" />
              </Field>
            </>
          )}

          {step === 1 && (
            <>
              <Field label="Provider">
                <div className="radio-row">
                  {(["codex", "devin", "antigravity", "grok", "opencode"] as ProviderId[]).map((p) => {
                    const accts = accounts.filter((a) => a.provider === p);
                    const free = accts.filter((a) => a.status === "active" && a.running < a.max_concurrent).length;
                    return (
                      <div key={p} className={`radio-card ${provider === p ? "sel" : ""}`} onClick={() => { setProvider(p); setAccountId("auto"); setModel(""); }}>
                        <div className="t mono">{p}</div>
                        <div className="d">{accts.length ? `${free}/${accts.length} accounts free` : "no accounts"}</div>
                      </div>
                    );
                  })}
                </div>
              </Field>
              <Field label="Account" hint='"auto" = scheduler LRU pick; naming an account pins it (409 account_busy if full)'>
                <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                  <option value="auto">auto — scheduler pick</option>
                  {provAccounts.map((a) => (
                    <option key={a.id} value={a.id} disabled={a.status !== "active"}>
                      {a.id} — {a.label} · {a.status} · {a.running}/{a.max_concurrent} slots{a.status !== "active" ? ` (${a.last_error ?? a.status})` : ""}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Model">
                <select value={model} onChange={(e) => setModel(e.target.value)}>
                  <option value="">provider default</option>
                  {models.map((m) => (
                    <option key={m.model} value={m.model} disabled={m.accounts_available === 0}>
                      {m.model} — {m.accounts_available} account{m.accounts_available === 1 ? "" : "s"} available
                    </option>
                  ))}
                </select>
              </Field>
            </>
          )}

          {step === 2 && (
            <>
              <Field label="Workspace repo (optional)" hint="Clone URL or filesystem path — declared as workspace.{repo, base_ref, base_sha}">
                <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="https://github.com/owner/repo" />
              </Field>
              {hasWorkspace && (
                <>
                  <div className="grid cols-2">
                    <Field label="base_ref"><input value={baseRef} onChange={(e) => setBaseRef(e.target.value)} /></Field>
                    <Field label="base_sha" hint="Exact 40-hex pin; a ref that resolves elsewhere fails run-1 as base_sha_mismatch">
                      <input value={baseSha} onChange={(e) => setBaseSha(e.target.value)} placeholder="b1783949…" className="mono" />
                    </Field>
                  </div>
                  <Field label="Git policy (optional)">
                    <div className="field"><input value={gitPolicy.branch} onChange={(e) => setGitPolicy({ ...gitPolicy, branch: e.target.value })} placeholder="work branch, e.g. sbx/flaky-date" /></div>
                    <div className="row small">
                      <label><input type="checkbox" checked={gitPolicy.push} onChange={(e) => setGitPolicy({ ...gitPolicy, push: e.target.checked })} /> push head to branch</label>
                      <label><input type="checkbox" checked={gitPolicy.auto_create_pr} onChange={(e) => setGitPolicy({ ...gitPolicy, auto_create_pr: e.target.checked })} /> auto_create_pr</label>
                      <label><input type="checkbox" checked={gitPolicy.draft} onChange={(e) => setGitPolicy({ ...gitPolicy, draft: e.target.checked })} /> draft PR</label>
                      <span className="muted">target:</span>
                      <input style={{ width: 90 }} value={gitPolicy.target} onChange={(e) => setGitPolicy({ ...gitPolicy, target: e.target.value })} />
                    </div>
                  </Field>
                  <Field label="Handoff (optional)" hint="Exactly one of artifact_id / head_sha / pull_request — reviewer-start pins a PR ref to an exact head">
                    <div className="radio-row">
                      {(["none", "artifact_id", "head_sha", "pull_request"] as const).map((k) => (
                        <div key={k} className={`radio-card ${handoffKind === k ? "sel" : ""}`} onClick={() => setHandoffKind(k)}>
                          <div className="t mono small">{k}</div>
                        </div>
                      ))}
                    </div>
                  </Field>
                  {handoffKind === "artifact_id" && (
                    <Field label="artifact_id">
                      <select value={handoffVal} onChange={(e) => setHandoffVal(e.target.value)}>
                        <option value="">pick a durable artifact…</option>
                        {ARTIFACTS.map((a) => <option key={a.artifact_id} value={a.artifact_id}>{a.artifact_id} — {a.repo}</option>)}
                      </select>
                    </Field>
                  )}
                  {handoffKind === "head_sha" && (
                    <Field label="head_sha"><input className="mono" value={handoffSha} onChange={(e) => setHandoffSha(e.target.value)} placeholder="40-hex, must descend from base_sha" /></Field>
                  )}
                  {handoffKind === "pull_request" && (
                    <div className="grid cols-2">
                      <Field label="ref"><input className="mono" value={handoffVal} onChange={(e) => setHandoffVal(e.target.value)} placeholder="refs/pull/42/head" /></Field>
                      <Field label="head_sha"><input className="mono" value={handoffSha} onChange={(e) => setHandoffSha(e.target.value)} placeholder="pinned head" /></Field>
                    </div>
                  )}
                </>
              )}
            </>
          )}

          {step === 3 && (
            <>
              <Field label="Output contract (JSON Schema, optional)" hint="Enforced subset only — pattern/$ref/if-then-else are refused as invalid_output_contract">
                <textarea value={schema} onChange={(e) => setSchema(e.target.value)} placeholder='{"type":"object","required":["fixed"]}' />
              </Field>
              {schema && (
                <Field label="Enforcement" hint="strict: invalid output → run ERROR + contract_violation. warn: FINISHED + diagnostic">
                  <div className="pill-toggle">
                    {(["strict", "warn"] as const).map((e) => (
                      <button key={e} className={enforcement === e ? "on" : ""} onClick={() => setEnforcement(e)}>{e}</button>
                    ))}
                  </div>
                </Field>
              )}
              <Field label="Workflow binding (optional)" hint="Persists on the agent; recoverable via GET /v1/workflows/{workflow_id} from any fresh client">
                <div className="grid cols-3">
                  <input value={wfId} onChange={(e) => setWfId(e.target.value)} placeholder="workflow_id" />
                  <input value={taskId} onChange={(e) => setTaskId(e.target.value)} placeholder="task_id" />
                  <input value={role} onChange={(e) => setRole(e.target.value)} placeholder="role" />
                </div>
              </Field>
            </>
          )}

          {step === 4 && (
            <>
              {problems.length > 0 ? (
                problems.map((p) => <div key={p} className="alert red">{p}</div>)
              ) : (
                <div className="alert blue">Ready — this request would create the sandbox and queue run 1.</div>
              )}
              <div className="small muted">Confirm the request body →</div>
            </>
          )}

          <div className="row mt" style={{ justifyContent: "space-between" }}>
            <button className="btn ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>← Back</button>
            {step < STEPS.length - 1 ? (
              <button className="btn primary" onClick={() => setStep(step + 1)}>Next →</button>
            ) : (
              <button className="btn primary" disabled={problems.length > 0} onClick={() => nav("/agents/ag_7f3k2")}>
                Create agent (mock)
              </button>
            )}
          </div>
        </div>

        <div>
          <Card title="POST /v1/agents — request preview">
            <JsonView value={payload} />
          </Card>
          <div className="card mt">
            <h3>Validation</h3>
            {problems.length === 0 ? (
              <div className="small" style={{ color: "var(--green)" }}>No blocking issues</div>
            ) : (
              problems.map((p) => <div key={p} className="small" style={{ color: "var(--red)", marginBottom: 6 }}>• {p}</div>)
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
