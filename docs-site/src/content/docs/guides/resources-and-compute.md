---
title: Compute and resources
description: Size an agent's sandbox, attach allow-listed Modal Secrets and MCP servers, and understand how long idle agents stay alive.
---

Each agent runs in its own Modal Sandbox. At create time you can size that
sandbox (`compute`) and attach named resources to it (`resources`). Both are
validated before any sandbox starts: a bad value is rejected, never clamped or
silently dropped.

## Size the sandbox

`compute` takes a `[request, limit]` pair per dimension. A single number sets
both.

| Field | Unit | Default | Allowed |
| --- | --- | --- | --- |
| `cpu` | cores | `[1, 2]` | 0.125 – 64 |
| `memory_mib` | MiB (integers) | `[1024, 8192]` | 128 – 262144 |

```json
{
  "prompt": { "text": "Build the project and run the full test suite" },
  "agent": { "provider": "codex" },
  "compute": { "cpu": [2, 4], "memory_mib": [4096, 16384] }
}
```

Omitted fields use the defaults. The request is what Modal reserves; the
sandbox may burst up to the limit. The resolved sizing is stored with the
agent — it is returned as `compute` on the agent record and survives
control-plane restarts and sandbox restores. Cost estimates
(`cost_estimate_usd`) are computed from the request values.

A non-numeric value, a request above its limit, or a value outside the allowed
range is `400 invalid_compute`.

## Attach Modal Secrets

`resources.secrets` lists Modal Secret **names** to mount into this agent's
sandbox only. The control plane passes names, never values: the Secret's
contents stay inside Modal and that one sandbox.

```json
{
  "prompt": { "text": "Run the integration tests against staging" },
  "agent": { "provider": "codex" },
  "resources": { "secrets": ["staging-db"] }
}
```

The operator decides which names are allowed with `SBX_RESOURCE_SECRETS` on
the control plane (comma-separated). The control plane's own credential
Secrets — account Secrets (`sbx-acct-*`), `sbx-codex-auth`, the HTTP Basic and
bootstrap-key Secrets, the GitHub Secret — can never be attached. An unknown
or disallowed name is `400 invalid_resource`.

## Attach MCP servers

`resources.mcp` lists names of MCP servers from the deployment's registry.
Only **devin** has an MCP channel; any other provider answers
`400 unsupported`.

The operator defines the registry as JSON in `SBX_MCP_REGISTRY`:

```json
{
  "issues": {
    "url": "https://mcp.example.com/issues",
    "transport": "http",
    "headers": { "Authorization": "Bearer ${env:ISSUES_TOKEN}" },
    "providers": ["devin"]
  }
}
```

| Key | Meaning |
| --- | --- |
| `url` | Server URL (required). |
| `transport` | Transport name, `http` by default. |
| `headers` | Extra headers. A header whose name looks like a credential (`Authorization`, `*token*`, `*secret*`, `*api-key*`, …) must take its value from `${env:VAR}`; entries with a literal credential are ignored. |
| `providers` | Optional list of providers the entry is offered to. |

Names are 1–64 characters of letters, digits, `_` and `-`. A malformed entry
cannot be referenced. The agent then asks for servers by name:

```json
{
  "prompt": { "text": "Triage the open issues labelled bug" },
  "agent": { "provider": "devin" },
  "resources": { "mcp": ["issues"], "secrets": ["issues-token"] }
}
```

Credential values never pass through the API or the generated MCP config —
they reach the sandbox as environment variables (for example from a Secret
attached with `resources.secrets`). The generated config lives outside the
workspace, so it never ends up in an artifact. Resource names — never values —
are echoed as `resources` on the agent record.

## How long agents stay alive

| Bound | Default | Setting | What happens |
| --- | --- | --- | --- |
| Idle retention | 300 s | `SBX_IDLE_TIMEOUT_S` | An `idle` agent with no new run is reclaimed by the reaper and becomes `timed_out`. |
| Sandbox idle timeout | 1800 s | `SBX_SANDBOX_IDLE_TIMEOUT_S` | Modal's own inactivity bound for a live sandbox, including during a long run. |
| Sandbox timeout | 14400 s (4 h) | `SBX_SANDBOX_TIMEOUT_S` | Hard cap on a sandbox's lifetime. |

These are deployment-wide settings — see
[Configuration](/operations/configuration/). `idle_timeout_s` on the create
request (integer ≥ 1) is accepted and stored with the agent, but idle agents
are currently reclaimed using the deployment-wide idle retention.

Close agents you no longer need with `DELETE /v1/agents/{id}`: an idle agent
keeps its sandbox — and its account slot — until it is closed or reclaimed.

## Errors

| Code | Status | Cause |
| --- | --- | --- |
| `invalid_compute` | 400 | Malformed or out-of-range `cpu` / `memory_mib`, or request above limit. |
| `invalid_resource` | 400 | Unknown or disallowed Secret name, or unknown MCP server name. |
| `unsupported` | 400 | MCP requested for a provider without an MCP channel. |
