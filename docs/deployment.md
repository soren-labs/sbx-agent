# Deployment guide

Everything below deploys into **your own Modal workspace**. You need:
Python ≥ 3.12, `uv`, the Modal CLI (`uv sync` provides it), and Modal
authentication — either an interactive login (`modal token new` once) or
both `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` exported in the environment.
`sbx doctor` reports which source it found (never the values) and prints
the remediation when neither is present.

## One-shot path (bootstrap CLI)

The 0.1 bootstrap CLI (`sbx`, SOR-98) is the documented route — run it as
`uv run sbx …` (equivalently `python -m sbx …`, or the `sbx` console script
once installed):

```bash
uv run sbx init       # checks python/uv/git/modal + Modal auth, writes local config
uv run sbx deploy     # builds runtime images, seeds Dicts/Secrets, deploys sbx-control
uv run sbx doctor     # verifies auth, secrets, control URL, /v1 auth, providers
uv run sbx smoke      # one minimal real run through /v1
```

The [manual path](#manual-path-what-sbx-deploy-wraps) below is exactly what
the CLI wraps; the first-run walkthrough with credential discovery is the
[README Quick Start](../README.md#quick-start).

It is idempotent — re-running `deploy`/`doctor` never damages state — and it
prints the two values clients need: `SBX_BASE_URL` and a `sbx_<key>` API key
(plaintext shown exactly once; the control plane stores `sha256(key)` only).

## What gets created

| Resource | Default name | Override |
| --- | --- | --- |
| Modal App (ASGI + reaper cron `*/5`) | `sbx-control` | `SBX_MODAL_APP_NAME` |
| Runtime images | `sbx-runtime`, `sbx-runtime-devin`, `sbx-runtime-antigravity`, `sbx-runtime-grok`, `sbx-runtime-opencode` | built by `make image*` / `sbx deploy` |
| Dicts | `sbx-sessions`, `sbx-runs`, `sbx-accounts`, `sbx-workflows`, `sbx-artifacts`, `sbx-workspaces` | created on demand by stores |
| Secrets | `sbx-basic-auth`, `sbx-v1-bootstrap`, `sbx-acct-<account_id>`, plus `sbx-codex-auth` **only when `codex` is enabled** | `modal secret create` |

Which providers a deployment serves is configured by `deploy.providers` in
the config file (env override `SBX_PROVIDERS`, comma-separated; default
`codex`). Deploy preconditions derive from that list: the shared Codex
credential Secret is required iff `codex` is enabled, and only enabled
providers' account Secrets are checked/materialized. An empty or unknown
provider list fails `sbx deploy` before anything is written.

Control-plane tunables (env on the Modal app): `SBX_MAX_CONCURRENT` caps
*live* agents/sandboxes — per key (default 2) and globally in the scheduler
(default 8). "Live" means `creating`/`idle`/`running`: an idle agent waiting
for a follow-up still occupies a slot until it is closed
(`DELETE /v1/agents/{id}`) or reaped. `sbx status`/`sbx doctor` show live
agents against the cap when configured (`deploy.max_concurrent` or the env
var); `429 concurrency_limit` remediation is closing idle agents, scoped
cleanup (`DELETE /v1/workflows/{id}`), or raising the cap and redeploying.
`SBX_IDLE_TIMEOUT_S` (default 1800 — idle reclaim, feeding both the
reaper sweep and the sandbox's native `idle_timeout`),
`SBX_TURN_MAX_SECONDS` (default 900 — runner `--max-seconds`; the reaper's
stranded-`running` bound is this plus `SBX_RUN_GRACE_S`, default 300),
`SBX_SANDBOX_TIMEOUT_S` (default 14400 — the Modal hard cap), and
`SBX_CREATE_GRACE_S` (default 300 — the in-flight create window). Each is
also settable as `deploy.<name>` in `$SBX_CONFIG`; the resolved values are
replayed into the remote functions' env at `sbx deploy`, so a configured
bound means the same thing to the runner, the reaper, and `Sandbox.create`.
Per-provider
`SBX_<PROVIDER>_SLOTS`, `SBX_<PROVIDER>_MODELS`, and multi-account fleets via
`SBX_<PROVIDER>_ACCOUNTS` (JSON list of `{id, label?, secret_name?, slots?,
models?}`). Devin's seeded account takes `SBX_DEVIN_BURST_SLOTS` (default 8).
`SBX_PROVIDERS` (comma list, default `codex`) selects which providers the app
serves — the shared `sbx-codex-auth` Secret is only required and mounted when
`codex` is selected, so e.g. a devin-only deploy does not need it.

### Optional GitHub bridge (SOR-117)

Sandboxes can clone/push **private** GitHub repos and open pull requests when
the control-plane env carries **both** `GH_TOKEN` (or `GITHUB_TOKEN`) **and**
the explicit opt-in `SBX_GITHUB_EPHEMERAL=1`. The token rides the ephemeral
Secret/exec env only — never a named Modal Secret, never argv, never disk —
and a `GIT_CONFIG_*` credential helper scoped to `https://github.com` answers
git's credential prompt at runtime. Without the gate nothing is injected and
public-repo workspaces are unaffected; PR creation then fails fast with
`repo_unavailable`. Least privilege: a fine-grained PAT limited to the agent
repositories with `Contents` (+ `Pull requests` if agents open PRs)
read/write and a short lifetime. `sbx doctor` reports the detected auth
source and gate state — never the token.

For a **remote** control plane (a `sbx deploy`ed Modal app — the token env
on the deploy host does not follow the function), store it as a Modal Secret
and name it via `SBX_GITHUB_SECRET_NAME`; `sbx deploy` fails fast if the
named Secret does not exist:

```bash
modal secret create sbx-github GH_TOKEN='<fine-grained PAT>'
export SBX_GITHUB_EPHEMERAL=1 SBX_GITHUB_SECRET_NAME=sbx-github
uv run sbx deploy
```

The full repo workflow — workspace declarations, GitHub-less fallbacks,
artifact handoffs, review pinning — is in
[docs/repo-workflow.md](repo-workflow.md).

## Manual path (what `sbx deploy` wraps)

```bash
# 1. Secrets — values never echoed, never committed
#    (sbx-codex-auth only when codex is an enabled provider)
modal secret create sbx-codex-auth CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"
modal secret create sbx-basic-auth \
  SBX_BASIC_USER='<user>' SBX_BASIC_PASS='<long-random>'
modal secret create sbx-v1-bootstrap \
  SBX_V1_BOOTSTRAP_KEY='sbx_<long-random-bootstrap-key>'

# 2. Runtime images (only the providers you use)
make image                # codex — sbx-runtime
make image-devin          # devin — sbx-runtime-devin
make image-antigravity    # needs your agy binary (SBX_AGY_BIN or ~/.local/bin/agy)
make image-grok           # needs your grok binary (SBX_GROK_BIN or ~/.local/bin/grok)
make image-opencode       # opencode — sbx-runtime-opencode (npm pin, no host binary)
make image-manifest       # provider → image/CLI/pin manifest as JSON (no Modal creds)

# 3. Control plane
make deploy               # = python -m modal deploy -m control.modal_app
```

`SBX_V1_BOOTSTRAP_KEY` seeds a hash-only admin API key plus the default
accounts on first boot (`control/api_v1/bootstrap.py`) — one seeded account
per **enabled** provider (`SBX_PROVIDERS`). Codex keeps the
shared `sbx-codex-auth` path; other providers get a seeded account pointing
at `sbx-acct-<id>` — create those Secrets with the credential blob, or import
accounts through the API/CLI instead (see [providers.md](providers.md)).

Import a provider account (writes account record + credential blob into
`sbx-accounts`; never prints material). On the next `sbx deploy` / `sbx upgrade`,
the bootstrap CLI materializes deployment-managed blobs into the matching
`sbx-acct-<account_id>` Modal Secret before the control app is deployed; you do
not need to create those account Secrets by hand:

```bash
uv run python -m control.onboarding --modal import \
  --provider devin --from ~/.local/share/devin/credentials.toml --slots 4
uv run python -m control.onboarding --modal list
```

Fetch your base URL from the deployed app (`modal app list` /
`modal app logs sbx-control`), then:

```bash
export SBX_BASE_URL='https://<workspace>--sbx-control-fastapi-app.modal.run'
export SBX_API_KEY='sbx_<the bootstrap key or a POST /v1/api-keys key>'
curl -H "Authorization: Bearer $SBX_API_KEY" $SBX_BASE_URL/v1/me
```

## Custom app name

`SBX_MODAL_APP_NAME=my-company-sbx uv run python -m modal deploy -m
control.modal_app` deploys a second, independent control plane. Dict and
Secret names are fixed contract names — parallel apps in one workspace share
them, so prefer separate Modal workspaces for parallel deployments.

## Upgrade

```bash
uv run sbx upgrade      # rebuilds images + redeploys; Dicts/Secrets are durable
```

Manual equivalent: `make image*` then `make deploy`. Durable runs, accounts,
workflow bindings and artifacts survive — they live in `modal.Dict`, not in
the deployment. In-flight sandboxes keep running on their existing image.

## Uninstall

```bash
uv run sbx uninstall                       # stops the app + leftover sandboxes
uv run sbx uninstall --purge-data          # also deletes the durable Dicts
uv run sbx uninstall --purge-credentials   # also deletes Secrets + the local key file
```

Manual equivalent:

```bash
modal app stop sbx-control
modal sandbox list         # verify zero sbx sandboxes remain
# optional data purge: delete the sbx-* Dicts (sbx-sessions, sbx-runs,
#   sbx-accounts, sbx-workflows, sbx-artifacts, sbx-workspaces) and Secrets
#   (sbx-codex-auth, sbx-basic-auth, sbx-v1-bootstrap, sbx-acct-*) from the
#   Modal dashboard or SDK.
```

Credentials are yours — uninstall never deletes them unless you pass
`--purge-credentials` (or `--purge-data` for the Dicts) or delete the
Secrets yourself.

## Optional edge

`deploy/sbx-edge/` is a Cloudflare Worker that fronts the control plane:
`/api/*` → Modal with Basic from Worker secrets, other paths → the static
`web/` dashboard. It is optional — `/v1` clients can hit the Modal URL
directly. Worker secrets (`SBX_BASIC_USER`/`SBX_BASIC_PASS`) are set via
`wrangler secret put` only.
