---
title: Configuration
description: Environment variables, config file, and tunables.
---

## Config file

Configuration lives in `~/.config/sbx/config.toml` and is created by `uv run sbx init`:

```toml
[deploy]
providers = ["codex", "devin"]
modal_app_name = "sbx-control"
max_concurrent = 8
idle_timeout_s = 300
turn_max_seconds = 900
sandbox_timeout_s = 14400
create_grace_s = 300
run_grace_s = 300


```

## Environment variables

Environment variables override config file values. Organized by category:

### Deploy & bootstrap

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_PROVIDERS` | `codex` | Comma-separated provider list |
| `SBX_MODAL_APP_NAME` | `sbx-control` | Modal App name |
| `SBX_STATE_DIR` | `~/.local/state/sbx` | Local state dir |
| `SBX_CONFIG` | `~/.config/sbx/config.toml` | Config file path |

### Lifecycle timers

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_IDLE_TIMEOUT_S` | `300` | Post-session idle retention (seconds) |
| `SBX_SANDBOX_IDLE_TIMEOUT_S` | `1800` | Native sandbox idle timeout |
| `SBX_TURN_MAX_SECONDS` | `900` | Per-turn timeout (runner `--max-seconds`) |
| `SBX_SANDBOX_TIMEOUT_S` | `14400` | Hard sandbox timeout (4 hours) |
| `SBX_CREATE_GRACE_S` | `300` | In-flight create window |
| `SBX_RUN_GRACE_S` | `300` | Stranded-running grace |

All must agree across runner, reaper, and native `Sandbox.create()`. The CLI resolves them once at deploy and injects them into remote functions.

### Concurrency

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_MAX_CONCURRENT` | per key `2`, global `8` | Live-agent caps: per API key and across the control plane; setting it gives both the same value |
| `SBX_<PROVIDER>_SLOTS` | `4` | Slot count of the account `sbx deploy` seeds for that provider |
| `SBX_DEVIN_BURST_SLOTS` | `8` | Default slot count of the seeded Devin account (overridden by `SBX_DEVIN_SLOTS`) |

### Accounts & credentials

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_ACCOUNT_SECRET_PREFIX` | `sbx-acct-` | Prefix for account Secret names |
| `SBX_CODEX_SECRET_NAME` | `sbx-codex-auth` | Codex shared credential Secret |
| `SBX_BASIC_SECRET_NAME` | `sbx-basic-auth` | Dashboard HTTP Basic Secret |
| `SBX_V1_BOOTSTRAP_SECRET_NAME` | `sbx-v1-bootstrap` | API bootstrap Secret |

### GitHub bridge

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_GITHUB_EPHEMERAL` | (disabled) | Enable GitHub bridge; set to `1` |
| `SBX_GITHUB_SECRET_NAME` | — | Modal Secret holding `GH_TOKEN` / `GITHUB_TOKEN` (remote deploys) |
| `SBX_GITHUB_APP_ID` | — | GitHub App ID |
| `SBX_GITHUB_APP_SLUG` | — | GitHub App slug (for install URL) |
| `SBX_GITHUB_APP_SECRET_NAME` | — | Modal Secret holding `SBX_GITHUB_APP_PRIVATE_KEY` |

For local deploys, export `GH_TOKEN` directly. For remote deploys, use Modal Secrets.

### Resources & MCP

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_RESOURCE_SECRETS` | — | Comma-separated allowlist of Secret names sandboxes can request |
| `SBX_MCP_REGISTRY` | — | JSON map of MCP server definitions (Devin only) |

Example:

```bash
export SBX_RESOURCE_SECRETS="sbx-api-keys,sbx-db-creds"
export SBX_MCP_REGISTRY='{"filesystem": {"command": "mcp-fs", "args": []}}'
```

### Provider CLI versions

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_<PROVIDER>_VERSION` | Pinned in `runtime/packages.txt` | Override provider CLI version; can be `latest` |
| `SBX_VERSIONS_LOCK` | — | Lock file (JSON) for reproducible builds |
| `SBX_VERSIONS_LOCK_OUT` | — | Output path for version lock (default: `$SBX_STATE_DIR/cli-versions.json`) |

Example (use latest):

```bash
export SBX_DEVIN_VERSION="latest"
uv run sbx deploy
```

Example (pin specific version):

```bash
export SBX_CODEX_VERSION="0.153.0"
uv run sbx deploy
```

Example (reproducible rollback):

```bash
uv run sbx deploy --versions-lock previous-lock.json
```

### Provider accounts & models

| Variable | Format | Purpose |
| --- | --- | --- |
| `SBX_<PROVIDER>_ACCOUNTS` | JSON array | Fleet: `[{"id": "...", "slots": ...}]` |
| `SBX_<PROVIDER>_MODELS` | Comma-separated | Default models for provider |

Example:

```bash
export SBX_DEVIN_ACCOUNTS='[{"id":"prod","slots":4},{"id":"test","slots":1}]'
export SBX_CODEX_MODELS="gpt-5.6-luna"
```

### Internal dashboard

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_API_USER` | `sbx` | HTTP Basic username |
| `SBX_API_PASSWORD` | `sbx` | HTTP Basic password (not a real secret, for dev only) |
| `SBX_SSE_KEEPALIVE_SECONDS` | `15` | Keepalive interval for SSE streams |

### Debugging

| Variable | Default | Purpose |
| --- | --- | --- |
| `SBX_RUNNER_CMD` | `python -m runtime.runner` | Runner entrypoint (testing only) |
| `SBX_DEVIN_TRANSPORT` | `acp` | Devin protocol: `acp` or `cli` |

## Configuration precedence

1. Command-line flags (if any)
2. Environment variables
3. Config file (`~/.config/sbx/config.toml`)
4. Defaults (code)
