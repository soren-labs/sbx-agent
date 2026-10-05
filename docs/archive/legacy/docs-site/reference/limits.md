---
title: Limits and timeouts
description: Lifecycle timers, concurrency caps, compute bounds and durable storage — with their defaults and the settings that change them.
---

Every value below is a control-plane setting: export it before
`./sbx deploy` and the deploy forwards it to the remote control plane.
See [Configuration](/self-hosting/configuration/).

## Lifecycle timers

`sbx deploy` resolves these once and injects the same values into the runner,
the reaper and `Sandbox.create()`, so they cannot drift apart.

| Timer | Setting | Default | What it bounds |
| --- | --- | --- | --- |
| Idle retention | `SBX_IDLE_TIMEOUT_S` | `300` (5 min) | How long an idle agent keeps its sandbox warm for a follow-up before the reaper reclaims it (`timed_out`). |
| Turn limit | `SBX_TURN_MAX_SECONDS` | `900` (15 min) | Maximum duration of one provider CLI turn. |
| Native sandbox idle | `SBX_SANDBOX_IDLE_TIMEOUT_S` | `1800` (30 min) | Modal's own inactivity kill while a sandbox is live. |
| Hard sandbox lifetime | `SBX_SANDBOX_TIMEOUT_S` | `14400` (4 h) | Absolute cap on a sandbox's lifetime. |
| Create grace | `SBX_CREATE_GRACE_S` | `300` (5 min) | How long an in-flight create may take before the reaper marks it `lost`. |
| Run grace | `SBX_RUN_GRACE_S` | `300` (5 min) | Margin after the turn limit before a `RUNNING` run with no live watcher is treated as stranded. |

The native sandbox idle bound is never allowed below
`SBX_TURN_MAX_SECONDS + SBX_RUN_GRACE_S`: a lower value is raised
automatically, so Modal cannot reclaim a sandbox in the middle of a long turn.

## Concurrency

| Cap | Setting | Default | When it is hit |
| --- | --- | --- | --- |
| Live agents per API key | `SBX_MAX_CONCURRENT` | `2` | `429 concurrency_limit` |
| Live agents across the control plane | `SBX_MAX_CONCURRENT` | `8` | `429 concurrency_limit` |
| Agents per account | the account's `max_concurrent` | see below | `429 provider_exhausted` with `account_id: "auto"`; `409 account_busy` for a named account |

`SBX_MAX_CONCURRENT` feeds both the per-key and the global cap; setting it
gives both the same value. An idle agent still holds its slot until it is
closed (`DELETE /v1/agents/{id}`) or reclaimed after the idle retention.

Per-account slots (`max_concurrent`) default to:

- `1` for accounts you import (`--slots` on `control.onboarding add`, or
  `max_concurrent` on `POST /v1/accounts`);
- `4` for accounts seeded at deploy time — `8` for Devin
  (`SBX_DEVIN_BURST_SLOTS`) — overridable with `SBX_<PROVIDER>_SLOTS` or a
  `slots` entry in `SBX_<PROVIDER>_ACCOUNTS`
  (see [Accounts](/guides/accounts/)).

`provider_exhausted` carries `retry_after` — the earliest expected recovery:
the remaining cooldown of a cooling account, or 60 s when accounts are simply
full. A pool whose accounts are all `invalid` or `disabled` gets no hint.

## Compute bounds

Per-agent `compute` values ([Compute and resources](/guides/resources-and-compute/)):

| Dimension | Default `[request, limit]` | Allowed range |
| --- | --- | --- |
| `cpu` (cores) | `[1, 2]` | `0.125` – `64` |
| `memory_mib` | `[1024, 8192]` | `128` – `262144` |

A request above its limit, a non-numeric value or a value out of range is
`400 invalid_compute`.

## Request limits

- `GET /v1/agents` returns at most 100 agents per page; follow `next_cursor`
  for more.
- The control plane has no request-rate limiter. The `429` responses it
  returns are capacity signals (`concurrency_limit`, `provider_exhausted`);
  honor `retry_after` and back off with jitter.
- Artifacts have no size or count cap. They are stored in your Modal
  workspace and count toward its storage.

## Durable storage

State lives in Modal Dicts in your workspace, so it survives sandbox teardown
and control-plane restarts:

| Dict | Holds |
| --- | --- |
| `sbx-sessions` | Agent records |
| `sbx-runs` | The run ledger: terminal status, structured errors, usage |
| `sbx-accounts` | Account registry (metadata; credentials live in Modal Secrets) |
| `sbx-workflows` | Workflow bindings |
| `sbx-artifacts` | Workspace snapshots and handoff packages |
| `sbx-workspaces` | Workspace and git state per agent |
| `sbx-checkpoints` | Recovery checkpoints for suspended agents |
| `sbx-github-app` | GitHub App installation metadata |
| `sbx-environments` | Environment build cache records |

## Example: tuning timers

```bash
# Long analysis turns: allow 30-minute turns.
export SBX_TURN_MAX_SECONDS=1800
export SBX_SANDBOX_IDLE_TIMEOUT_S=2100   # ≥ 1800 + SBX_RUN_GRACE_S (300)
./sbx deploy
```

```bash
# Short interactive work: reclaim idle agents after one minute.
export SBX_TURN_MAX_SECONDS=300
export SBX_IDLE_TIMEOUT_S=60
./sbx deploy
```
