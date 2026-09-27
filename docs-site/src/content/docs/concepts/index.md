---
title: Concepts
description: Key terms and the agent lifecycle.
---

## Agent

A **long-lived stateful container** running an official provider CLI. Created by `POST /v1/agents`, an agent has a unique ID and persists across multiple turns until explicitly closed (`DELETE /v1/agents/{id}`).

Each agent:
- Runs in its own Modal Sandbox
- Mounts a credential for one provider account
- Maintains a native session across turns (e.g., Codex's `thread_id`)
- Has a durable status: `creating`, `idle`, `running`, `closed`, `timed_out`, `lost`

### Agent statuses

| Status | Meaning |
| --- | --- |
| `creating` | Sandbox is launching (usually < 10 seconds) |
| `idle` | Sandbox is alive and ready for the next run |
| `running` | A run is in progress on this agent |
| `closed` | Agent was explicitly closed (`DELETE /v1/agents/{id}`) |
| `timed_out` | Agent was idle longer than `SBX_IDLE_TIMEOUT_S` (default 5 min) |
| `lost` | The Modal Sandbox vanished (infrastructure failure) |

:::note
Terminal statuses (`closed`, `timed_out`, `lost`) never change once written. A closed agent's run history is read-only.
:::

## Run

A **single execution** on an agent, initiated by `POST /v1/agents/{id}/runs` or created implicitly by `POST /v1/agents`. Each run has a unique ID and a durable terminal state.

Runs stream canonical events (ISO 8601 timestamps, LF-delimited JSON) over SSE, with `Last-Event-ID` resumption if disconnected.

### Run statuses

| Status | Meaning |
| --- | --- |
| `CREATING` | Run is preparing (provisioning sandbox if needed) |
| `RUNNING` | Provider CLI is executing the prompt |
| `FINISHED` | Run completed successfully with output |
| `ERROR` | Run failed with a structured error (auth, timeout, etc.) |
| `CANCELLED` | Run was cancelled by `POST /v1/agents/{id}/runs/{runId}/cancel` |
| `EXPIRED` | Run did not finish before the agent's timeout |
| `UNKNOWN` | Outcome unknown (no persisted terminal state, sandbox lost) |

### Run errors

Every terminal `ERROR` or `EXPIRED` run includes structured `error`:

```json
{
  "code": "auth_invalid|rate_limited|quota_exhausted|timeout|cancelled|...",
  "source": "provider|runtime|control|telemetry",
  "message": "human-readable description",
  "retryable": true,
  "retry_after": 45
}
```

Error codes include: `auth_invalid`, `rate_limited`, `quota_exhausted`, `model_unavailable`, `model_capacity`, `provider_unavailable`, `runtime_error`, `event_parse_error`, `timeout`, `cancelled`, `contract_violation`.

## Provider

An official coding agent vendor (Codex, Devin, Antigravity, Grok, OpenCode). sbx-browser drives each provider's official CLI inside the sandbox, never making API calls directly.

### Supported providers

| Provider | Status | CLI | Multi-turn | Multi-account |
| --- | --- | --- | --- | --- |
| Codex | Stable | `@openai/codex` 0.153.0 | ✅ `codex exec resume` | ✅ `SBX_CODEX_ACCOUNTS` |
| Devin | Experimental | Devin 3000.10.21 | ✅ ACP session | ✅ `SBX_DEVIN_ACCOUNTS` |
| Antigravity | Experimental | `agy` 1.2.3+ | ✅ `--conversation` | ✅ `SBX_ANTIGRAVITY_ACCOUNTS` |
| Grok | Experimental | `grok` 1.0.24+ | ✅ `--resume` | ✅ `SBX_GROK_ACCOUNTS` |
| OpenCode | Experimental | `opencode-ai` 1.18.29 | ✅ `--session` | ✅ `SBX_OPENCODE_ACCOUNTS` |

Full evidence and per-provider notes: [Provider support](/integrations/providers/).

## Account

A credential for one provider (e.g., your Codex account, your Devin subscription). Accounts are imported once and scheduled automatically across agents.

- Each account has ID, provider, slots (max concurrent agents), and status (`active`, `cooling`, `invalid`, `disabled`).
- `account_id: "auto"` picks the least-recently-used account that is active and has free slots.
- On `auth_invalid` or `rate_limited`, an account enters cooldown and is skipped; the scheduler picks another account.

## Sandbox

A Modal Sandbox: the isolated VM where the provider CLI runs. One per agent, created on demand, with:
- Durable `/work` directory (survives turndown and restore)
- Ephemeral credential files injected at mode 0600
- No Modal tokens or platform credentials
- Lifecycle bounds: idle timeout (post-session), native sandbox timeout (mid-turn), hard timeout (4 h)

The sandbox is the **only security boundary**. Provider CLI sandboxing is disabled because the VM provides containment.

## Workspace

A git repository state: base repo, ref, and sha. Agents operating on a workspace record the base checkout, make durable changes (via artifact patches or direct commits), and support review workflows.

## Artifact

A durable package tying a workspace state to an output checkpoint:

- `manifest.json`: metadata (repo, base_sha, etc.)
- `patch.diff`: incremental changes
- `repo.bundle`: full bundle (for remote checkouts)
- `files/`: individual file snapshots
- SHA256 content hash for verification
- Secret scan (fail-closed on leaked tokens)

Used for cross-agent handoff via `handoff` (`{"artifact_id": "…"}` or `{"head_sha": "…"}`).

## Workflow

A logical grouping of agents and runs under one `workflow_id`. Persists across process restarts for recovery.

- Agents in a workflow carry `metadata.workflow_id`
- `GET /v1/workflows/{id}` reads durable progress
- `DELETE /v1/workflows/{id}` is idempotent scoped cleanup
- Recover with `SbxClient.recover(workflow_id)` after a crash

## Durable vs. ephemeral

| Durable (survives sandbox teardown, control-plane restart) | Ephemeral (sandbox-local, lost on close) |
| --- | --- |
| Run ledger (`FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`, structured error) | Event stream (`events.jsonl`) |
| Session records (agent status, session_id) | Raw CLI output (`events.raw.jsonl`) |
| Account registry (credential blobs, status, slots) | `$HOME` credential files |
| Workflow bindings (agent metadata, run refs) | Worktree and agent workspace state |
| Artifacts (packages, manifest, patches) | - |

**Consequence:** Terminal run states never change once persisted. A vanished sandbox can make a run `UNKNOWN` but never silently upgrade it to `FINISHED`.

## SSE & event streaming

Runs stream events as **Server-Sent Events (SSE)**. Each event has:
- **ID** — `events.jsonl` line number (use in `Last-Event-ID` header to resume)
- **Type** — canonical event name (`turn.started`, `item.completed`, `sbx.turn_finished`, etc.)
- **Data** — JSON payload

Keepalive (`: keepalive`) fires every 15 seconds to detect stale connections. If disconnected, reconnect with `Last-Event-ID: <last-received-id>` and the stream resumes from the next event.

On repeated disconnections, fall back to `GET /v1/agents/{id}/runs/{runId}` to read the durable ledger.

## API key & scopes

API keys are Bearer tokens (`Authorization: Bearer sbx_<key>`) with optional scopes:

- `agents` (default) — create, read, cancel runs; list models
- `admin` — manage accounts, API keys, and credential verification

The control plane stores `sha256(key)` only; plaintext is shown once at creation.
