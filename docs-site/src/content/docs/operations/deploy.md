---
title: Deploying sbx-browser
description: Bootstrap, deploy, verify, and scale your control plane.
---

## One-shot deployment (recommended)

The `sbx` CLI handles the entire deployment:

```bash
uv run sbx init --providers codex,devin      # check env, write config
uv run sbx deploy                             # build, push, deploy
uv run sbx doctor                             # verify all systems
```

This is what happens under the hood:

## What gets created

| Resource | Name | Purpose |
| --- | --- | --- |
| **Modal App** | `sbx-control` (env: `SBX_MODAL_APP_NAME`) | FastAPI + reaper cron |
| **Runtime images** | `sbx-runtime`, `sbx-runtime-devin`, etc. | Per-provider sandbox images |
| **Dicts** | `sbx-sessions`, `sbx-runs`, `sbx-accounts`, `sbx-workflows`, `sbx-artifacts`, `sbx-workspaces` | Durable state |
| **Secrets** | `sbx-codex-auth`, `sbx-basic-auth`, `sbx-v1-bootstrap`, `sbx-acct-<id>` | Credentials |

## Step-by-step deployment

<Steps>

1. **Check prerequisites:**
   ```bash
   uv run sbx init --providers codex,devin
   ```
   Verifies Python, uv, git, Modal auth, and scans for installed provider CLIs.

2. **Import provider credentials:**
   ```bash
   modal secret create sbx-codex-auth \
     CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"
   
   uv run python -m control.onboarding --modal import \
     --provider devin --from ~/.local/share/devin/credentials.toml \
     --account-id devin-1
   ```

3. **Build runtime images:**
   ```bash
   # Done automatically by `sbx deploy`, or manually:
   make image
   make image-devin
   ```

4. **Deploy the control plane:**
   ```bash
   uv run sbx deploy
   ```
   Outputs:
   - `SBX_BASE_URL` — control plane endpoint
   - `sbx_<key>` — API key (shown once)

5. **Verify deployment:**
   ```bash
   uv run sbx doctor
   ```
   Should show all systems green.

</Steps>

## Configuration

Customize deployment via `~/.config/sbx/config.toml` or environment variables:

```toml
[deploy]
providers = ["codex", "devin"]
modal_app_name = "sbx-control"
max_concurrent = 8
idle_timeout_s = 300
turn_max_seconds = 900
sandbox_timeout_s = 14400
```

Env overrides:

```bash
export SBX_PROVIDERS="codex,devin"
export SBX_MODAL_APP_NAME="my-sbx"
export SBX_MAX_CONCURRENT="16"
export SBX_DEVIN_SLOTS="4"
uv run sbx deploy
```

## Scaling

### Concurrency cap

`SBX_MAX_CONCURRENT` sets both live-agent caps — per API key (default 2)
and across the control plane (default 8):

```bash
export SBX_MAX_CONCURRENT="8"
uv run sbx deploy
```

When the cap is hit, new creates return `429 concurrency_limit`. Remediation: close idle agents or increase the cap.

### Per-account slots

Limit concurrency per provider account:

```bash
export SBX_DEVIN_ACCOUNTS='[{"id":"devin-prod","slots":4}]'
uv run sbx deploy
```

### Provider selection

Deploy only the providers you need:

```bash
export SBX_PROVIDERS="codex,devin"
uv run sbx deploy
```

Unselected providers are not built or scheduled. The shared `sbx-codex-auth` Secret is required only when `codex` is enabled.

## Upgrades

Re-deploy to pick up code changes:

```bash
git pull
uv sync
uv run sbx upgrade
```

Upgrade preserves durable Dicts (runs, accounts, workflows, artifacts stay intact). Images are rebuilt, and the control plane restarts.

## Manual deployment (advanced)

For custom setups, manually build and deploy:

```bash
# Build images
make image
make image-devin

# Create Secrets
modal secret create sbx-codex-auth CODEX_AUTH_JSON="..."
modal secret create sbx-v1-bootstrap ...
modal secret create sbx-basic-auth ...

# Deploy
python -m control.modal_app
```

See the `Makefile` for the complete build pipeline.

## Uninstall

```bash
uv run sbx uninstall
```

Stops the app and closes all sandboxes. By default, durable Dicts are preserved. To erase:

```bash
uv run sbx uninstall --purge-data
uv run sbx uninstall --purge-credentials
```

Durable data (runs, artifacts, workflows) persists unless you explicitly purge.

## Verification

```bash
uv run sbx doctor
```

Checks:
- Modal authentication
- Secrets and Dicts present
- Control plane is running (checking `/v1/me`)
- Provider auth probes pass
- Sandbox creation works (`sbx smoke`)

## Troubleshooting

**Deploy fails at image build:**
```
Check that all provider CLIs are installed locally. Run:
uv run sbx credentials --verify
```

**Sandbox creation timeout:**
```
Modal may be slow. Try again. If persistent, check capacity in
modal logs --tail --all
```

**Control plane not responding:**
```
Check Modal app status:
modal app list
modal app logs sbx-control --tail
```
