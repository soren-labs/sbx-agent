# Architecture

sbx-browser is three tiers: a thin client surface, a FastAPI control plane on
Modal, and one Modal Sandbox per agent running the official provider CLI.

```
 client / CI                      your Modal workspace
 ┌──────────────┐   /v1 Bearer   ┌─────────────────────────────────────────┐
 │ sbx_client   │ ─────────────▶ │ sbx-control (FastAPI, scales to 0)      │
 │ curl · your  │                │  api_v1/  public REST (/v1/*)           │
 │ orchestrator │                │  app.py   internal dashboard API (/api/*│
 └──────────────┘                │           HTTP Basic, for web/)         │
                                 │  scheduler + account registry          │
                                 │  run ledger + workflow index           │
                                 │  reaper cron (*/5 min)                 │
                                 │ stores: modal.Dict                     │
                                 │  sbx-sessions / sbx-runs /             │
                                 │  sbx-accounts / sbx-workflows          │
                                 └────────────────┬────────────────────────┘
                                                  │ Sandbox.create(image,
                                                  │  secrets, idle_timeout,
                                                  │  timeout, cpu, mem)
                                                  ▼
                                    ┌─── Modal Sandbox (1 per agent) ────┐
                                    │ entrypoint.sh                      │
                                    │  $SBX_WORK=/work, HOME=/work/home  │
                                    │ runner (python -m runtime.runner)  │
                                    │  init · turn · stop ·              │
                                    │  export-credentials                │
                                    │ AgentAdapter → official CLI        │
                                    │  codex · devin · agy · grok        │
                                    └────────────────────────────────────┘
```

## Request lifecycle

1. `POST /v1/agents {prompt, agent:{provider, account_id|auto, model}}` —
   the scheduler picks a free account (`auto` = LRU with slot caps), creates
   a sandbox on the provider's image, mounts the account Secret, and queues
   run 1. The call may return while the run is still `CREATING` (async
   create); poll `GET .../runs/{runId}` or watch the SSE stream.
2. The control plane `sb.exec`s `runner init` (writes `$HOME`, credential
   files `0600`, `session.json`, empty `events.jsonl`) then `runner turn`
   per run. The CLI's stdout is translated to canonical events and appended
   to `events.jsonl` — the same lines SSE replays, 1-indexed.
3. `POST .../runs` on an idle agent is a follow-up turn resuming the native
   provider session (`exec resume` / `--resume` / `--conversation` / ACP).
4. `POST .../cancel` SIGTERMs the in-flight CLI (30 s grace → SIGKILL).
   `DELETE /v1/agents/{id}` closes the agent and reclaims the sandbox;
   history stays read-only.
5. The reaper (every 5 min) reconciles the session Dict against live
   sandboxes: idle past `idle_timeout_s` → `timed_out`; vanished → `lost`;
   expired account cooldowns return to rotation.

## Durable vs ephemeral

| Durable (survives teardown) | Ephemeral (sandbox-local) |
| --- | --- |
| Run ledger (`sbx-runs`): status, terminal `RunError`, usage, artifact refs | `events.jsonl`, `events.raw.jsonl` |
| Session records (`sbx-sessions`) | `inbox/<n>.md`, `turns/<n>.json` |
| Account registry + credential blobs (`sbx-accounts`) | `$HOME` credential files |
| Workflow bindings (`sbx-workflows`, agent `metadata`) | the worktree itself |
| Artifact packages (manifest + patch/bundle, sha256) | |

Consequences:

- **Terminal run states never change once persisted** (`FINISHED` / `ERROR` /
  `CANCELLED` / `EXPIRED`). A vanished sandbox can make a run's outcome
  `UNKNOWN` — never silently `FINISHED`.
- **SSE ids are `events.jsonl` line numbers.** `Last-Event-ID` resumes after
  a drop; when the sandbox is gone the ledger is the source of truth and the
  client falls back from `watch` to `wait`.
- **Workflow recovery** needs only `API key + workflow_id`: agents carry
  persisted `metadata.workflow_id`, so `GET /v1/workflows/{id}` (or
  `client.recover`) rebuilds the agent/run/artifact view for a fresh process.

## Security model

- The **Modal Sandbox is the sole execution boundary**. Provider CLIs run
  with their own sandboxing disabled (`--dangerously-bypass-approvals-and-sandbox`
  for codex) because the provider's Landlock/seccomp sandbox is unreliable
  under gVisor; containment is the VM, not the CLI.
- Sandboxes contain **no Modal token and no platform credentials** — only the
  workdir and the provider's own credential files.
- Credential material moves as a `{"provider", "files": {relpath: content}}`
  blob (`SBX_ACCOUNT_CREDENTIAL` env inside the Secret), is restored at
  `0600`, and is stripped from the provider CLI's child environment
  (`shell_environment_policy.exclude` for codex; adapter-level scrub for the
  others — including `ACP_BACKEND`/`DEVIN_*`/`WINDSURF_*` for devin and
  `GROK_*`/`XAI_*` for grok).
- `/v1` auth is `Bearer sbx_<key>`; the control plane stores `sha256(key)`
  only. Internal `/api/*` uses HTTP Basic and exists for the bundled
  dashboard — it is not a public surface.
- Artifact collection fails closed on secret material (`409 artifact_secret`).

## Contracts

Cross-component interfaces are frozen per release and tested for consistency
(`tests/unit/test_contract_consistency.py`):

| Contract | Covers |
| --- | --- |
| [docs/contracts/filesystem.md](contracts/filesystem.md) | `$SBX_WORK` layout, `HOME`, `CODEX_HOME` |
| [docs/contracts/events.md](contracts/events.md) | canonical events, `sbx.*` runner events, usage fields |
| [docs/contracts/runner-cli.md](contracts/runner-cli.md) | `runner init/turn/stop/export-credentials`, exit codes |
| [docs/contracts/api.yaml](contracts/api.yaml) | internal `/api/*` (Basic) |
| [docs/contracts/api-v1.yaml](contracts/api-v1.yaml) | public `/v1/*` (Bearer) — OpenAPI 3.1 |
| [docs/contracts/artifacts.md](contracts/artifacts.md) | artifact package format, handoff, review pinning |

## Layout

```
control/     sbx-control: FastAPI, scheduler, ledger, stores, Modal backend
runtime/     sandbox side: image recipe (packages.txt), entrypoint, runner,
             per-provider AgentAdapters
web/         build-less dashboard for the internal /api surface
examples/    sbx_client.py — reference /v1 client
deploy/      optional edge (Cloudflare Worker) in front of sbx-control
tests/       unit · integration · e2e (mocked) · e2e_modal (real, host-run)
spike/       recorded provider experiments (evidence for the support matrix)
docs/        this file, deployment/provider guides, frozen contracts
```
