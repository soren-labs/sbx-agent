---
title: Accounts and credentials
description: Import provider logins as accounts, size their slots, and understand how the scheduler picks, cools down and fails over between them.
---

An **account** is one provider login — a subscription you already pay for —
registered with the control plane. Agents run under an account: the
scheduler picks one when the agent is created and mounts that account's
credential into the agent's sandbox, and nowhere else.

The control plane keeps only account **metadata** (provider, label, status,
slots, models). The credential itself is an opaque blob
`{"provider": …, "files": {relpath: content}}` restored under the sandbox's
`$HOME`; on Modal it lives in the `sbx-accounts` Dict and is materialized as a
per-account Modal Secret named `sbx-acct-<id>`.

## Accounts created at deploy time

`sbx deploy` seeds one account per selected provider (`SBX_PROVIDERS`), so a
fresh deployment can run agents immediately:

| Provider | Default account id | Credential |
| --- | --- | --- |
| codex | `codex-1` | the shared `sbx-codex-auth` Secret (`CODEX_AUTH_JSON`) |
| devin, antigravity, grok, opencode | `<provider>-1` | the account's `sbx-acct-<id>` Secret |

Tune the seeded account per provider with `SBX_<PROVIDER>_ACCOUNT_ID`,
`SBX_<PROVIDER>_SECRET_NAME`, `SBX_<PROVIDER>_SLOTS` and
`SBX_<PROVIDER>_MODELS` (comma-separated). To seed a **pool** of several
logins for one provider, set `SBX_<PROVIDER>_ACCOUNTS` to a JSON list:

```bash
export SBX_GROK_ACCOUNTS='[
  {"id": "grok-1", "slots": 2},
  {"id": "grok-2", "slots": 2, "label": "team grok"}
]'
uv run sbx deploy
```

Each entry takes `id` (required) and optionally `label`, `secret_name`
(default `sbx-acct-<id>`), `slots` and `models`.

## Import an account

Import a provider login from your machine with the onboarding CLI. `--modal`
targets your Modal deployment; without it the command manages a local
control plane's file store.

```bash
uv run python -m control.onboarding --modal import \
  --provider devin --from ~/.local/share/devin/credentials.toml \
  --account-id devin-2 --label "devin team" --slots 2
```

| Flag | Meaning |
| --- | --- |
| `--provider` | `codex`, `devin`, `antigravity`, `grok` or `opencode` (required) |
| `--from` | Credential file, directory or blob; `-` reads stdin (required) |
| `--account-id` | Account id; generated when omitted |
| `--label` | Display label |
| `--slots` | Per-account slot count (`max_concurrent`), default `1` |
| `--models` | Comma-separated models this account advertises |
| `--allow-open-permissions` | Accept credential files readable by group or other |

The import validates the blob before storing it: unknown providers, a
provider/blob mismatch, undeclared or escaping paths, symlinks and files
readable by group or other are refused. Run `uv run sbx deploy` afterwards so
the account's `sbx-acct-<id>` Secret is created. Credential file locations per
provider are listed in [Providers](/integrations/providers/); `uv run sbx
credentials` finds the ones on your machine.

Other onboarding commands: `providers`, `list [--provider P]`, `status ID`,
`verify ID`, `refresh ID --from SRC` (replace the stored credential),
`export ID --out PATH` (write it to a `0600` file), `disable ID` / `enable
ID`, and `remove ID --yes` (refused while the account has running agents).

### Over the API

Admins can also register accounts over HTTP (the console's **Admin →
Accounts** page uses these calls):

```http
POST /v1/accounts
Authorization: Bearer sbx_...
Content-Type: application/json

{
  "provider": "devin",
  "label": "devin team",
  "max_concurrent": 2,
  "credential": {
    "files": { ".local/share/devin/credentials.toml": "…file contents…" }
  }
}
```

The account id is generated (`acct-<provider>-<hex>`). Credential contents
are never returned by any endpoint. `GET /v1/accounts`,
`GET /v1/accounts/{id}` and `DELETE /v1/accounts/{id}` list, read and remove
accounts; all account endpoints need the `admin` scope.

## Verify an account

```bash
uv run python -m control.onboarding --modal verify devin-2 --probe auth
```

`--probe auth` restores the credential in a throwaway sandbox and runs the
provider CLI's own auth check, so the provider decides whether the login is
still valid. `--probe sandbox` only proves the credential restores and the
runner accepts it. Over the API, `POST /v1/accounts/{id}/verify` probes the
stored credential in a throwaway sandbox: a failure marks the account
`invalid`, a pass marks it `active`.

## How agents get an account

With `"account_id": "auto"` (the default), the scheduler picks the
least-recently-used `active` account of the requested provider that has a
free slot. With a named account — `"agent": {"account_id": "devin-2"}` — it
uses exactly that one or refuses.

| Response | When |
| --- | --- |
| `429 provider_exhausted` | `auto`: no account of the provider is `active` with a free slot. Carries `retry_after`. |
| `409 account_unavailable` | Named account is missing, belongs to another provider, or is not `active`. |
| `409 account_busy` | Named account has no free slot. |
| `429 concurrency_limit` | The control plane's live-agent cap is reached ([Limits](/reference/limits/#concurrency)). |

Slots free up when an agent is closed or reclaimed — an idle agent keeps its
slot until then.

## Account status and failover

| Status | Meaning |
| --- | --- |
| `active` | Schedulable. |
| `cooling` | Temporarily skipped after a capacity failure; returns to `active` by itself when the cooldown ends. |
| `invalid` | The provider rejected the login. Refresh the credential and verify again. |
| `disabled` | Turned off by an operator (`onboarding disable`). |

A run's structured error feeds back into its account:

- `rate_limited`, `quota_exhausted`, `provider_unavailable` or
  `model_capacity` → `cooling` for the provider's `retry_after`, or 15
  minutes (`SBX_ACCOUNT_COOLDOWN_S`, default `900`) when none is given;
- `auth_invalid` → `invalid`;
- runtime, control-plane and telemetry errors are recorded on the account
  without changing its status.

Subsequent `auto` agents simply land on another account of the pool.

## Refreshed logins are written back

Provider CLIs rotate OAuth tokens inside the sandbox. After each run (and
once more when the agent closes) the control plane exports the CLI's
credential files and, if they changed, commits them to the account and
updates its Modal Secret in place — no redeploy. A newer credential is never
overwritten by an older copy from a slower sandbox. Set
`SBX_CRED_WRITEBACK=0` to turn write-back off.

To replace a login yourself, sign in again locally and run
`onboarding refresh ID --from <file>`; a refresh that fails validation leaves
the last good credential untouched.
