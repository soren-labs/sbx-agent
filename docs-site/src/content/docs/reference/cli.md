---
title: CLI
description: sbx command-line interface and bootstrap tools.
---

## sbx CLI

All commands run as `uv run sbx <cmd>` or `python -m sbx <cmd>`. Global flags `--config`, `--state-dir`, and `--json` apply to all commands.

### sbx init

Check prerequisites, validate credentials, and write config:

```bash
uv run sbx init --providers codex,devin --verify
```

**Flags:**
- `--providers PROVIDERS` — comma-separated provider list (codex, devin, antigravity, grok, opencode)
- `--profile PROFILE` — Modal profile name
- `--app-name APP_NAME` — Modal app name for the control plane
- `--base-url BASE_URL` — public API base URL (for external access)
- `--verify` — run each provider CLI's own auth check (slow but authoritative)
- `--allow-open-permissions` — accept credential files readable by group/other (unsafe)
- `--github, --no-github` — persist GitHub auth bridge config
- `--github-secret NAME` — Modal Secret name holding GH_TOKEN

**Checks:**
- Python ≥ 3.12
- `uv`, `git`, `modal` CLIs present
- Modal authentication (MODAL_TOKEN_ID and MODAL_TOKEN_SECRET or login)
- Installed provider CLIs (unless --providers limits scope)

### sbx credentials

Scan and verify provider credentials:

```bash
uv run sbx credentials --verify
```

**Flags:**
- `--providers PROVIDERS` — comma-separated providers to scan (default: configured providers)
- `--verify` — run each provider CLI's auth check (slow, authoritative)
- `--allow-open-permissions` — accept credential files readable by group/other
- `--json` — JSON output

**Output:**
- `discovered` — credential file found
- `permission_invalid` — file readable by group/other (insecure)
- `auth_invalid` — credential failed provider auth check
- `verified` — credential passed auth check

### sbx config

Show the resolved non-sensitive config:

```bash
uv run sbx config
```

`config` takes no subcommands.

### sbx status

Show deployment status:

```bash
uv run sbx status --json
```

Shows whether the control plane is running and reports the base URL and available accounts.

### sbx deploy

Build and deploy the control plane:

```bash
uv run sbx deploy
```

**Flags:**
- `--versions-lock PATH` — replay a frozen CLI versions lock file (rollback to earlier deployment's provider CLI versions)
- `--json` — JSON output

**Output:**
- `SBX_BASE_URL` — control plane endpoint
- `sbx_<key>` — Bearer API key (shown once; save it!)

### sbx doctor

Verify deployment health:

```bash
uv run sbx doctor
```

**Checks:**
- Modal authentication
- Secrets and Dicts exist
- Control plane responds (/v1/me)
- Provider auth probes
- Sandbox creation works

**Flags:**
- `--verify` — run each provider CLI's auth check
- `--allow-open-permissions` — accept credential files readable by group/other
- `--json` — JSON output

### sbx smoke

Run a minimal end-to-end test:

```bash
uv run sbx smoke --provider codex --timeout 300
```

**Flags:**
- `--provider PROVIDER` — provider to smoke (default: first configured)
- `--prompt PROMPT` — smoke prompt text (default: built-in prompt)
- `--timeout TIMEOUT` — seconds to wait for terminal (default: 600)
- `--poll POLL` — poll interval in seconds (default: 3)
- `--json` — JSON output

Creates a test agent, waits for completion, prints final status and cost.

### sbx upgrade

Update to a new version:

```bash
uv run sbx upgrade
```

**Flags:**
- `--version VERSION` — version string to record (default: package version)
- `--versions-lock PATH` — replay a frozen CLI versions lock file
- `--json` — JSON output

Rebuilds images and restarts the control plane; preserves durable data.

### sbx open

Open the web console in a browser:

```bash
uv run sbx open
uv run sbx open --print   # print the URL instead of launching a browser
```

Mints a one-time grant and hands off to the Console already signed in — no
copying API keys into the UI.

**Flags:**
- `--base-url URL` — control-plane URL (default: configured `api_base_url`, else the deployed app URL)
- `--print` — print the one-time Console URL instead of opening a browser
- `--json` — JSON output

### sbx github connect

Connect the GitHub integration:

```bash
uv run sbx github connect
uv run sbx github connect --print   # print the install URL
```

Opens the official GitHub App installation page (one click). The GitHub App
is the default bridge — a PAT (`github-secret`) works as a fallback; see
[GitHub integration](/integrations/github/).

**Flags:**
- `--base-url URL` — control-plane URL (default: configured / deployed URL)
- `--print` — print the GitHub install URL instead of opening a browser
- `--json` — JSON output

### sbx auth

Manage provider account authentication (login, capture, verify):

```bash
uv run sbx auth status
uv run sbx auth login --provider devin
uv run sbx auth pair <ticket>
```

| Command | Purpose |
| --- | --- |
| `status` | Local credential scan + per-account auth-session states (`--provider` filters) |
| `login --provider P` | Run the provider's official CLI/OAuth login, then capture and verify |
| `import-existing` | Capture credential files a vendor login already wrote — no token paste (`--from`, `--account-id`, `--slots`, `--models`, `--no-verify`, `--experimental`) |
| `verify ACCOUNT_ID` | Run the cloud auth probe; promotes the account on success (`--local` forces the sandbox probe) |
| `relink` | Re-capture → refresh → verify; restores scheduler eligibility after a dead grant (`--from`, `--login` runs the official login first, `--no-verify`) |
| `pair TICKET` | Pair this host's provider login with a cloud connect session from the Console (`--base-url` overrides the target) |
| `logout` | Sign out: drop credential material, flip the account to unverified (`--delete-files` also removes the provider's files under `$HOME`, `--keep-secret` preserves the managed Modal Secret) |

This is the same flow the Console's **Integrations → Connect provider**
page drives; `sbx auth pair <ticket>` is the local-pairing path it offers.

### sbx uninstall

Stop and remove the deployment:

```bash
uv run sbx uninstall
uv run sbx uninstall --purge-data --purge-credentials
```

**Flags:**
- `--purge-data` — also delete durable Dicts (sessions, runs, accounts, workflows, artifacts, workspaces)
- `--purge-credentials` — also delete account Secrets and the local key file
- `--json` — JSON output

## Global flags (all commands)

| Flag | Effect |
| --- | --- |
| `--json` | JSON output (when supported) |
| `--config CONFIG` | Override config file path (SBX_CONFIG or XDG default) |
| `--state-dir STATE_DIR` | Override state directory (SBX_STATE_DIR or XDG default) |

## Bootstrap tools

### control.onboarding

Import and manage provider accounts:

```bash
uv run python -m control.onboarding add \
  --provider devin \
  --from ~/.local/share/devin/credentials.toml \
  --account-id devin-1

uv run python -m control.onboarding verify \
  devin-1 --probe auth
```

**Global flags:**
- `--store-dir STORE_DIR` — file store root (local mode, default: XDG)
- `--modal` — use Modal Dicts instead of local files

**Commands:**

| Command | Purpose |
| --- | --- |
| `providers` | List supported provider descriptors |
| `add` (alias: `import`) | Import credential file as a new account |
| `verify ACCOUNT_ID` | Probe stored credential (static, sandbox, or auth seam) |
| `list` | List accounts (metadata only, never credentials) |
| `status ACCOUNT_ID` | Get one account descriptor |
| `refresh ACCOUNT_ID` | Atomically replace stored credential (OAuth write-back) |
| `export ACCOUNT_ID` | Write stored blob to a 0600 file |
| `enable ACCOUNT_ID` | Re-enable account for scheduling |
| `disable ACCOUNT_ID` | Temporarily disable account |
| `remove ACCOUNT_ID` | Delete account and credential |

**Command flags:**

`add --provider P --from SRC`:
- `--provider PROVIDER` — provider name (required)
- `--label LABEL` — display label for the account
- `--from SOURCE` — credential file/dir/blob, or `-` for stdin (required)
- `--account-id ACCOUNT_ID` — account ID (auto-generated if omitted)
- `--slots SLOTS` — max concurrent agents for this account
- `--models MODELS` — comma-separated advertised models
- `--experimental` — allow experimental providers
- `--allow-open-permissions` — accept insecure file permissions

`verify ACCOUNT_ID`:
- `--probe {static,sandbox,auth}` — `static` = schema check; `sandbox` = runner init restore check; `auth` = provider CLI auth check (authoritative)
- `--runner-cmd RUNNER_CMD` — runner argv prefix (sandbox probe)

`refresh ACCOUNT_ID`:
- `--from SOURCE` — new credential blob (required)
- `--allow-open-permissions` — accept insecure file permissions

`export ACCOUNT_ID`:
- `--out PATH` — output file path (required; never writes to stdout)

`remove ACCOUNT_ID`:
- `--yes` — skip confirmation (required to actually delete)

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Error (check stderr for details; `--json` output includes `error.code` and `hint`) |

## Examples

```bash
# Full bootstrap with verification
uv run sbx init --providers codex,devin --verify
uv run sbx deploy
uv run sbx doctor
uv run sbx smoke

# Upgrade preserving durable data
git pull
uv sync
uv run sbx upgrade

# Full cleanup
uv run sbx uninstall --purge-data
```

## Makefile targets

The repository includes common development and deployment commands via `make`:

```bash
make lint       # ruff check + ruff format --check
make test       # pytest (unit + integration, no cloud credentials)
make test-e2e   # playwright smoke tests (mocked providers)
make console-dev # serve the console + local /v1 control plane at :8790
make docs-dev   # start Astro docs dev server
make docs-build # build docs site for production
make deploy     # deploy to Modal workspace (requires config)
```
