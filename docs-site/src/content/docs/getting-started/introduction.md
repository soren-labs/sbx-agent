---
title: Introduction
description: What sbx-browser is, who it's for, architecture overview, and API surfaces.
---

## What is sbx-browser?

sbx-browser is **self-hosted orchestration for cloud coding agents.** It lets you run official provider CLIs (Codex, Devin, Antigravity, Grok, OpenCode) inside isolated [Modal](https://modal.com) Sandboxes, coordinated by a single REST API that you control.

Think of it as:
- **One REST API** (`/v1`, Bearer authenticated) in front of many agents
- **One sandbox per agent**, long-lived and resumable across turns
- **Durable run ledger** — terminal states persist across sandbox teardown and control-plane restarts
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
│ Your client / CI         │   POST /v1/agents, GET /v1/agents/{id}/runs/{runId}/stream
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

- **One agent = one sandbox.** A sandbox persists across turns and is reclaimed when idle (default 5 min post-session) or explicitly closed.
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
POST   /v1/agents                    Create an agent + start run 1
GET    /v1/agents                    List agents
GET    /v1/agents/{id}               Get agent details
DELETE /v1/agents/{id}               Close an agent
POST   /v1/agents/{id}/runs          Create a follow-up run
GET    /v1/agents/{id}/runs          List runs
GET    /v1/agents/{id}/runs/{runId}  Get run details & structured output
GET    /v1/agents/{id}/runs/{runId}/stream  SSE: canonical events + keepalive
POST   /v1/agents/{id}/runs/{runId}/cancel  Cancel a run
GET    /v1/models                    List supported models per provider
GET    /v1/me                        Current API key info
```

Plus accounts, workflows, artifacts, and git/PR orchestration. Full schema: [API reference](/reference/api/).

### `/api/*` — Internal API (HTTP Basic, legacy)

The web console calls `/v1`; `/api` is kept for internal/legacy callers only. Not a public surface; all external integrations must use `/v1`.

## Status and known limitations

**Version: v0.1.1 — Public alpha.**

- The `/v1` API shape may evolve before 1.0.
- All five providers (codex, devin, antigravity, grok, opencode) have passed real-account gates on Modal. See [Provider support](/integrations/providers/) for the detailed matrix and evidence.
- Single workspace per deployment (multi-workspace architecture is not yet supported).
- No browser UI (the web console is an internal dashboard, no terminal/noVNC).

## What's next?

→ [Quick start](/getting-started/quick-start/): Clone, configure, and deploy.  
→ [Concepts](/concepts/): Agent, Run, Provider, Sandbox, Workflow, and more.  
→ [Creating agents](/guides/agents-and-runs/): Your first multi-turn run.
