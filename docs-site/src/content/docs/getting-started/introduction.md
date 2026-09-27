---
title: Introduction
description: What sbx-browser is, who it's for, architecture overview, and API surfaces.
---

## What is sbx-browser?

sbx-browser is **self-hosted orchestration for cloud coding agents.** You describe a **task** — a prompt, optionally a repository and where the result should go — and sbx-browser runs an official provider CLI (Codex, Devin, Antigravity, Grok, OpenCode) inside an isolated [Modal](https://modal.com) Sandbox, then delivers the result as a durable revision you can publish, review and merge.

Think of it as:
- **One REST API** (`/v1`, Bearer authenticated) in front of many tasks
- **One task → one sandboxed agent**, resumable across follow-up runs
- **Durable ledger** — task status, run terminal states, revisions and reviews persist across sandbox teardown and control-plane restarts
- **Multi-turn with native sessions** — the official provider CLI's resume mechanism works natively
- **Multi-account pools** — schedule work across your own accounts with LRU, slots, and failover

### Who is it for?

- **Self-hosted teams** deploying a control plane for internal coding-agent orchestration
- **Integration builders** needing reproducible runs, artifact handoff, and workflow recovery
- **Multi-agent workflows** that need durable checkpointing and cross-agent coordination

### What it is *not*

- **Not a model API.** You bring your own official provider subscriptions; sbx-browser does not proxy, resell, or convert quota.
- **Not a hosted service.** Everything runs in *your* Modal workspace, billed to you, with *your* credentials.
- **Not a SaaS account store.** We do not host accounts, store credentials, or call provider APIs directly.

## Architecture

Every deployment has three layers:

```
┌──────────────────────────┐
│ Your client / CI         │   POST /v1/tasks, GET /v1/agents/{id}/runs/{runId}/stream
│ curl, Python SDK         │   Authorization: Bearer sbx_<key>
└──────────┬───────────────┘
           │ HTTPS
           ▼
┌──────────────────────────────────────────────────┐
│ sbx-control (FastAPI, your Modal workspace)      │
│ • Run ledger & scheduler                         │
│ • Account registry & failover (LRU + cooldown)   │
│ • Reaper cron (every 5 min)                      │
│ • Durable Dicts: sbx-sessions, sbx-runs,         │
│   sbx-accounts, sbx-workflows, sbx-artifacts     │
└──────────┬───────────────────────────────────────┘
           │ Sandbox.create()
           │ (one per agent)
           ▼
┌──────────────────────────────────────────────────┐
│ Modal Sandbox                                    │
│ • entrypoint.sh → runner (Python)                │
│ • Official provider CLI (codex/devin/etc.)       │
│ • Credential files (0600, no Modal token)        │
│ • Durable /work (ephemeral $HOME/agent state)    │
└──────────────────────────────────────────────────┘
```

**Key design choices:**

- **One task = one sandboxed agent.** A sandbox persists across turns and is reclaimed when idle (default 5 min post-session) or explicitly closed.
- **The sandbox is the security boundary.** Provider CLIs run with their own sandboxing disabled (via `--dangerously-bypass-approvals-and-sandbox` for codex) because the Modal VM is the true isolation. Credentials are stored as Modal Secrets and injected only into the sandbox.
- **Durable by default.** Run terminal states, workflow metadata, and artifacts survive sandbox teardown and control-plane restarts via `modal.Dict`.

## Lifecycle & timeouts

| Timeout | Default | What happens |
| --- | --- | --- |
| Post-session idle (`SBX_IDLE_TIMEOUT_S`) | 5 min (300 s) | Agent becomes `timed_out` when idle this long; sandbox is torn down |
| Native sandbox idle (`SBX_SANDBOX_IDLE_TIMEOUT_S`) | 30 min (1800 s) | Modal's native `Sandbox.idle_timeout=` — bounds the sandbox mid-turn |
| Hard limit (`SBX_SANDBOX_TIMEOUT_S`) | 4 h (14400 s) | Modal hard cap; any run exceeding this fails with `timeout` |

:::tip
The control plane's reaper reconciles actual idle time vs persisted agent status every 5 minutes. A long-running turn will not be interrupted if the turn's max duration is accounted for in the sandbox idle timeout (these are coordinated at deployment time).
:::

## API surfaces

### `/v1` — Public REST API (Bearer, recommended)

The official surface for external integration. Cursor Cloud Agents compatible shape:

```
POST   /v1/tasks                     Create a task + queue run 1
POST   /v1/tasks/preflight           Advisory resolution check (no side effects)
GET    /v1/tasks                     List tasks
GET    /v1/tasks/{id}                Task detail (status, delivery, latest revision)
POST   /v1/tasks/{id}/runs           Queue a follow-up run
GET    /v1/tasks/{id}/runs           List the task's runs
POST   /v1/tasks/{id}/cancel         Cancel a task
POST   /v1/tasks/{id}/retry          Re-run, or retry a failed delivery
GET    /v1/tasks/{id}/revisions      Durable results (branches/PRs/diffs)
POST   /v1/tasks/{id}/deliver        Push the branch + open the PR
POST   /v1/tasks/{id}/reviews        Record a sha-pinned review verdict
POST   /v1/tasks/{id}/merge          Review-gated merge of the delivered PR
GET    /v1/models                    List supported models per provider
GET    /v1/me                        Current API key info
```

Plus the agent-level surface (`/v1/agents/*`) tasks resolve onto — accounts,
workflows, artifacts, and git/PR orchestration. Full schema:
[API reference](/reference/api/).

### `/api/*` — Internal API (HTTP Basic, legacy)

The web console calls `/v1`; `/api` is kept for internal/legacy callers only. Not a public surface; all external integrations must use `/v1`.

## Status and known limitations

**Version: v0.1.1 — Public alpha.**

- The `/v1` API shape may evolve before 1.0.
- All five providers (codex, devin, antigravity, grok, opencode) have passed real-account gates on Modal. See [Provider support](/integrations/providers/) for the detailed matrix and evidence.
- Single workspace per deployment (multi-workspace architecture is not yet supported).
- The web console is an operator dashboard (tasks, integrations, keys) — there is no in-browser terminal or desktop.

## What's next?

→ [Quick start](/getting-started/quick-start/): Use an existing deployment or self-host.  
→ [Concepts](/concepts/): Task, Run, Revision, Provider, Sandbox, and more.  
→ [Tasks](/guides/tasks/): Create, monitor and deliver your first task.
