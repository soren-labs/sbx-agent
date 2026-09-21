import React from "react";
import { Badge, Card, Empty, ErrorCard } from "../ui";

// Gallery of edge/empty/error states — every card renders a canonical ErrorBody.
const ERRORS: { http: number; code: string; message: string; retry_after?: number; when: string }[] = [
  { http: 400, code: "invalid_provider", message: "provider \"claude\" is not schedulable in this deployment", when: "POST /v1/agents with an unregistered provider" },
  { http: 400, code: "invalid_output_contract", message: "schema uses unsupported keyword \"pattern\"", when: "output_contract with non-enforced keywords" },
  { http: 401, code: "unauthorized", message: "missing or invalid Bearer key", when: "any /v1 call without a valid sbx_ key" },
  { http: 403, code: "forbidden", message: "scope \"admin\" required", when: "agents-scoped key calling /v1/accounts" },
  { http: 404, code: "not_found", message: "agent ag_xxx not found", when: "deleted agent, wrong id, or another key's agent" },
  { http: 404, code: "workspace_not_found", message: "agent declared no workspace", when: "GET …/workspace on a workspace-less agent" },
  { http: 409, code: "turn_in_progress", message: "agent already has a running turn", when: "POST …/runs or …/handoff while running" },
  { http: 409, code: "account_busy", message: "account codex-1 has no free slot (2/2)", when: "create with a pinned, saturated account" },
  { http: 409, code: "base_sha_mismatch", message: "base_ref resolved to a different commit", when: "workspace pin drifted — agent closed, run-1 ERROR" },
  { http: 409, code: "head_sha_mismatch", message: "pull_request ref drifted from its pinned head_sha", when: "reviewer-start handoff on a moved ref" },
  { http: 409, code: "artifact_secret", message: "snapshot found credential-shaped content — nothing persisted", when: "artifact collection fails closed" },
  { http: 429, code: "provider_exhausted", message: "no free account for provider grok", retry_after: 300, when: "auto pick with all accounts cooling" },
  { http: 429, code: "concurrency_limit", message: "global live-agent cap reached (8)", retry_after: 60, when: "create while at cap" },
];

const RUN_ERRORS = [
  { code: "auth_invalid", source: "provider", message: "credential rejected by provider CLI auth check", retryable: false },
  { code: "rate_limited", source: "provider", message: "provider 429; retry_after=300", retryable: true, retry_after: 300 },
  { code: "contract_violation", source: "control", message: "final message failed output contract (strict)", retryable: false },
  { code: "timeout", source: "runtime", message: "runner exceeded turn timeout", retryable: true },
  { code: "event_parse_error", source: "telemetry", message: "event stream parse failed — not downgradable to warning", retryable: false },
];

export default function States() {
  return (
    <div>
      <h1>Edge states</h1>
      <div className="sub">
        Canonical error shapes from <code>api-v1.yaml</code> — HTTP errors share{" "}
        <code>{"{error:{code,message,retry_after?}}"}</code>; run failures persist a structured{" "}
        <code>RunError{"{code,source,message,retryable,retry_after?}"}</code>.
      </div>

      <div className="grid cols-2 section">
        <Card title="Empty states">
          <Empty title="No agents yet" hint="POST /v1/agents creates a sandbox and queues run 1." />
          <div className="mt" />
          <Empty title="Credential not ready" hint="Provider has no verified account — import one via POST /v1/accounts, then verify it in a throwaway sandbox." />
        </Card>
        <Card title="Credential-unready">
          <div className="alert amber">
            <strong><code>codex-2</code> — auth_invalid.</strong> The provider CLI's own check
            rejected the blob. Re-login locally (<code>codex login</code>), re-import, or probe:{" "}
            <code>POST /v1/accounts/codex-2/verify</code>. Runs pinned to this account get{" "}
            <code>409 account_unavailable</code>; <code>auto</code> just skips it.
          </div>
          <div className="alert amber">
            <strong><code>agy-1</code> — cooling until 14:02.</strong> <code>rate_limited</code>{" "}
            cooldown; scheduler skips it; <code>accounts_available</code> for its models reads 0.
          </div>
        </Card>
      </div>

      <h2>HTTP error bodies</h2>
      <div className="grid cols-3 section">
        {ERRORS.map((e) => (
          <div key={e.code}>
            <ErrorCard http={e.http} body={{ error: e }} />
            <div className="small muted" style={{ marginTop: -6, marginBottom: 10 }}>{e.when}</div>
          </div>
        ))}
      </div>

      <h2>RunError — persisted terminal failures</h2>
      <div className="grid cols-3">
        {RUN_ERRORS.map((e) => (
          <div key={e.code} className="card">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <code style={{ color: "var(--red)" }}>{e.code}</code>
              <Badge tone={e.retryable ? "amber" : "red"}>{e.retryable ? "retryable" : "terminal"}</Badge>
            </div>
            <div className="small muted mt">source: {e.source}</div>
            <div className="small">{e.message}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
