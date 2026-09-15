# Deployment guide

Everything below deploys into **your own Modal workspace**. You need:
Python ≥ 3.12, `uv`, the Modal CLI (`uv sync` provides it), and
`modal token new` completed once.

## One-shot path (bootstrap CLI)

The 0.1 bootstrap CLI (`sbx`, SOR-98) is the documented route:

```bash
sbx init       # checks python/uv/git/modal, writes local config, picks profile
sbx deploy     # builds runtime images, seeds Dicts/Secrets, deploys sbx-control
sbx doctor     # verifies auth, secrets, control URL, /v1 auth, providers
sbx smoke      # one minimal real run through /v1
```

It is idempotent — re-running `deploy`/`doctor` never damages state — and it
prints the two values clients need: `SBX_BASE_URL` and a `sbx_<key>` API key
(plaintext shown exactly once; the control plane stores `sha256(key)` only).

## What gets created

| Resource | Default name | Override |
| --- | --- | --- |
| Modal App (ASGI + reaper cron `*/5`) | `sbx-control` | `SBX_MODAL_APP_NAME` |
| Runtime images | `sbx-runtime`, `sbx-runtime-devin`, `sbx-runtime-antigravity`, `sbx-runtime-grok` | built by `make image*` / `sbx deploy` |
| Dicts | `sbx-sessions`, `sbx-runs`, `sbx-accounts`, `sbx-workflows` | created on demand by stores |
| Secrets | `sbx-codex-auth`, `sbx-basic-auth`, `sbx-v1-bootstrap`, `sbx-acct-<account_id>` | `modal secret create` |

Control-plane tunables (env on the Modal app): `SBX_MAX_CONCURRENT` (global
cap, default 2), `SBX_IDLE_TIMEOUT_S` (default 1800), per-provider
`SBX_<PROVIDER>_SLOTS`, `SBX_<PROVIDER>_MODELS`, and multi-account fleets via
`SBX_<PROVIDER>_ACCOUNTS` (JSON list of `{id, label?, secret_name?, slots?,
models?}`). Devin's seeded account takes `SBX_DEVIN_BURST_SLOTS` (default 8).

## Manual path (what `sbx deploy` wraps)

```bash
# 1. Secrets — values never echoed, never committed
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

# 3. Control plane
make deploy               # = python -m modal deploy -m control.modal_app
```

`SBX_V1_BOOTSTRAP_KEY` seeds a hash-only admin API key plus the default
accounts on first boot (`control/api_v1/bootstrap.py`). Codex keeps the
shared `sbx-codex-auth` path; other providers get a seeded account pointing
at `sbx-acct-<id>` — create those Secrets with the credential blob, or import
accounts through the API/CLI instead (see [providers.md](providers.md)).

Import a provider account (writes account record + credential blob into
`sbx-accounts`; never prints material):

```bash
python -m control.accounts --modal import \
  --provider devin --from ~/.local/share/devin/credentials.toml --slots 4
python -m control.accounts --modal list
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
sbx upgrade      # rebuilds images + redeploys; Dicts/Secrets are durable
```

Manual equivalent: `make image*` then `make deploy`. Durable runs, accounts,
workflow bindings and artifacts survive — they live in `modal.Dict`, not in
the deployment. In-flight sandboxes keep running on their existing image.

## Uninstall

```bash
sbx uninstall              # stops the app + leftover sandboxes
sbx uninstall --purge      # also deletes Dicts/Secrets (opt-in; off by default)
```

Manual equivalent:

```bash
modal app stop sbx-control
modal sandbox list         # verify zero sbx sandboxes remain
# optional data purge: delete the sbx-* Dicts (sbx-sessions, sbx-runs,
#   sbx-accounts, sbx-workflows) and Secrets (sbx-codex-auth, sbx-basic-auth,
#   sbx-v1-bootstrap, sbx-acct-*) from the Modal dashboard or SDK.
```

Credentials are yours — uninstall never deletes them unless you pass
`--purge` or delete the Secrets yourself.

## Optional edge

`deploy/sbx-edge/` is a Cloudflare Worker that fronts the control plane:
`/api/*` → Modal with Basic from Worker secrets, other paths → the static
`web/` dashboard. It is optional — `/v1` clients can hit the Modal URL
directly. Worker secrets (`SBX_BASIC_USER`/`SBX_BASIC_PASS`) are set via
`wrangler secret put` only.
