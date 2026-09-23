# Deployment bootstrap (`sbx` CLI) — SOR-98 / Release 0.1

> **Moved to the documentation website.** The maintained, user-facing version of this
> page is `operations/deploy` and `reference/cli` in [`docs-site/`](../docs-site/) (`make docs-dev`).
> This file stays as an engineering reference and may lag behind.

`sbx` takes a clean checkout to a callable `/v1` control plane on the user's
own Modal workspace. Run it as `uv run sbx …`, `python -m sbx …`, or the
`sbx` console script after install. The full first-run walkthrough is the
[README Quick Start](../README.md#quick-start); this file is the per-command
reference.

## Flow

```bash
git clone https://github.com/soren-labs/sbx-browser.git && cd sbx-browser
uv sync
uv run modal token new                         # or export MODAL_TOKEN_ID + MODAL_TOKEN_SECRET
uv run sbx init --profile <modal-profile>      # checks toolchain + Modal auth, writes config
uv run sbx credentials --verify                # discover local logins, verify via provider CLIs
modal secret create sbx-codex-auth \
  CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"  # only when codex is enabled
uv run python -m control.onboarding --modal import \
  --provider <p> --from <credential-file>      # other providers: account import
uv run sbx deploy                              # idempotent: secrets/dicts/image/app
uv run sbx doctor                              # end-to-end verification
uv run sbx smoke                               # minimal agent → terminal → cleanup
uv run sbx upgrade                             # redeploy, durable stores preserved
uv run sbx uninstall                           # stop app + terminate sbx sandboxes
```

## Configuration

One file is the single source: `$SBX_CONFIG`, else
`$XDG_CONFIG_HOME/sbx/config.toml`, else `~/.config/sbx/config.toml`. It holds
Modal profile/app name, durable Dict names, Secret names, provider image
pins, and `api.base_url`. Every value can be overridden by an env var
(`SBX_MODAL_PROFILE`, `SBX_MODAL_APP_NAME`, `SBX_BASE_URL`, `SBX_*_DICT`,
`SBX_*_SECRET_NAME`, `SBX_IMAGE_*`, `SBX_PROVIDERS`, …) — env wins over file,
file wins over built-in defaults (which come from `control.config`).

The optional GitHub auth bridge persists here too: `[github] ephemeral`
(the `SBX_GITHUB_EPHEMERAL` opt-in gate) and `[github] secret_name` (the
*name* of the Modal Secret holding `GH_TOKEN`, from `SBX_GITHUB_SECRET_NAME`).
`sbx init --github --github-secret sbx-github` writes them; the token value
is never persisted or printed anywhere.

Local state lives under `$SBX_STATE_DIR` / `$XDG_STATE_HOME/sbx`:
`bootstrap.key` (the `sbx_` API key, mode 0600), `basic-auth.json`
(0600, for the internal `/api/*` board), and `deploy.json` (last deploy
record). The control plane only stores `sha256` of the API key; the
plaintext exists only locally.

## Command semantics

| Command | Behavior |
| --- | --- |
| `init` | Check Python/uv/git/Modal CLI and Modal auth (profile login or `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` — a partial pair fails with remediation); scan selected providers' local credentials (advisory); report the advisory `github` bridge state against the resolved config; write config (idempotent; flags override file values — `--github`/`--no-github` and `--github-secret NAME` persist the GitHub bridge). |
| `credentials` | Scan the selected providers' declared credential files under `$HOME` and report presence/permissions/schema/status — never contents. `--verify` runs each provider CLI's own auth check (`codex login status`, `devin auth status`, `agy models`, `grok models`, `opencode auth list`); `--providers a,b` overrides the selection; `--allow-open-permissions` accepts files readable by group/other (default requires `0600`, with `chmod 600` remediation in the hint). |
| `config` | Print resolved non-sensitive config with per-value source (file/env/default). |
| `status` | Print deploy record, base URL, key fingerprint (`sha256:` prefix), provider view aggregated per provider (`devin: 1 account, 2 models`), live agents vs `SBX_MAX_CONCURRENT` when configured, and the resolved GitHub bridge state (`armed`/`off` + Secret name). |
| `deploy` | Preflight (provider config, Modal auth, provider-aware Secrets: `sbx-codex-auth` only when `codex` is in `deploy.providers`, plus enabled providers' referenced account Secrets, plus the named GitHub bridge Secret when `[github]` is armed) → bootstrap/basic Secrets → durable Dicts → runtime image(s) → `modal deploy` → `/v1/me` probe. Every step is check-then-act; reruns converge. |
| `doctor` | Modal auth, provider config + provider-required Secrets, durable Dicts, local key fingerprint, `/v1` reachability + auth, provider availability aggregated per provider/account, live agents vs the `SBX_MAX_CONCURRENT` cap (idle agents hold slots until closed), sandbox-list capability, advisory local credential scan (`--verify` upgrades it to provider-CLI auth checks) and advisory `github` bridge detection against the resolved config (`--verify` probes `gh auth status`; an armed bridge's named Secret is presence-checked like other prerequisites). Never prints secret values. |
| `smoke` | `POST /v1/agents` with a trivial prompt → poll the run to a terminal status → `DELETE` the agent. A non-`FINISHED` terminal run surfaces the canonical `run.error` fields (`code`/`source`/`message`/`retryable`/`retry_after`) — e.g. `auth_invalid` — re-clipped before printing. |
| `upgrade` | Snapshot all durable Dicts → redeploy → verify each is still readable with no lost keys. Aborts before touching anything when a store is unreadable. |
| `uninstall` | Terminate all sandboxes owned by the app, re-list to prove zero leftovers, stop the app. `--purge-data` also deletes Dicts; `--purge-credentials` also deletes Secrets (incl. `sbx-acct-*`), the local credential files, and the deploy record. Defaults preserve both. |

All commands accept `--json` (machine-readable output and error objects),
`--config`, and `--state-dir`. Errors carry a stable `code` plus an
actionable `hint` (`error[code]: message` on stderr, exit 1).

## After deploy

`deploy` prints the two exports `examples/sbx_client.py` needs:

```bash
export SBX_BASE_URL=<printed URL>
export SBX_API_KEY=$(cat "${XDG_STATE_HOME:-$HOME/.local/state}/sbx/bootstrap.key")
uv run python examples/sbx_client.py "Write hello.txt containing hi"
```

## Key rotation

The local key file and the `sbx-v1-bootstrap` Secret must stay in sync —
control stores only the hash, so a lost local file means the remote value is
unrecoverable. `sbx deploy` self-heals: mint a fresh local key (delete
`bootstrap.key` first), and deploy rotates the Secret to match.

## Tests

`tests/unit/sbx/` covers the whole surface against `FakePlane` (in-memory
Plane) and an `httpx.MockTransport` `/v1` — no Modal credentials, no
network, fully deterministic.
