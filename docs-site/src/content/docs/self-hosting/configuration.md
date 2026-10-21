---
title: Configuration
description: Every environment variable of the SBX control plane, with defaults and how to run it.
---

The control plane reads its configuration from the environment at start-up.
`sbx server init` writes the same variables to a private config file that every
`sbx server` command, `sbx serve` and `sbx migrate` read; environment variables
override it. See [Run a local control plane](/self-hosting/local-server/).

```bash
sbx server start                               # background, from the config file
sbx serve --host 127.0.0.1 --port 8800        # API + workers in one process (foreground)
python -m control.composition serve            # same thing, environment only
python -m control.composition worker           # workers only
python -m control.composition migrate          # apply migrations and exit
```

Each command applies pending migrations first, under an advisory lock. `serve`
runs `SBX_WORKER_THREADS` job workers and a one-minute timer that releases idle
executors.

## Required

| Variable | Meaning |
| --- | --- |
| `SBX_DATABASE_URL` | PostgreSQL connection string. The only business authority. |
| `SBX_VAULT_KEYS` | Credential keyring, `KID:BASE64KEY,KID2:BASE64KEY2`. The first key encrypts; older keys still decrypt after rotation. Keep it outside the database and backups. |
| `SBX_RUNTIME_MASTER_KEY` | Hex-encoded master key from which per-lease runtime keys are derived. Not stored in the database. |

The process exits at start-up if any of these is missing or malformed: each vault
key must decode to 32 bytes, the master key must be at least 32 bytes of hex, and
`SBX_EXECUTORS` may name only `local` and `modal`. The message names the variable
and the fix, never its value.

## Optional

| Variable | Default | Meaning |
| --- | --- | --- |
| `SBX_DATA_DIR` | `./.sbx-data` | Local blobs, git work area, local executor state and the mail sink. |
| `SBX_PUBLIC_URL` | `http://localhost:8800` | Base URL used in verification and reset links. |
| `SBX_ALLOWED_ORIGINS` | the public URL | Comma-separated origins allowed for cookie mutations. |
| `SBX_COOKIE_SECURE` | `0` | Set to `1` to mark cookies `Secure` (use behind HTTPS). |
| `SBX_EXECUTORS` | `local,modal` | Enabled executor backends. |
| `SBX_INFERENCE_ALLOW_PRIVATE_URLS` | unset | `1` lets inference Connections use private-network or plain-`http` base URLs (self-hosted gateways). Leave unset on multi-user deployments. |
| `SBX_RESEND_API_KEY` | unset | Send email through Resend. Without it, mail is written to `$SBX_DATA_DIR/mail/` (directory `0700`, files `0600`). |
| `SBX_MAIL_FROM` | unset | Sender address for Resend email, on a domain verified in Resend. Required when `SBX_RESEND_API_KEY` is set; startup fails without it. |
| `SBX_WORKER_THREADS` | `4` | In-process job worker threads. |

## Server config file

These variables are read by the `sbx server` commands (and `sbx serve`), not by
`python -m control.composition`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SBX_SERVER_CONFIG` | `$XDG_CONFIG_HOME/sbx/server.env` | The config file to use (same as `--config`). The started process receives it too. |
| `SBX_LISTEN_HOST` | `127.0.0.1` | Listen address for `sbx server start` and `sbx serve`. |
| `SBX_LISTEN_PORT` | `8800` | Listen port. |
| `SBX_MANAGED_POSTGRES` | unset | `1` when `SBX_DATABASE_URL` is the private cluster in `$SBX_DATA_DIR/postgres`; `start` and `stop` manage it. |

With a config file, `SBX_DATA_DIR` defaults to `$XDG_DATA_HOME/sbx`. The file holds
`KEY=VALUE` lines of the variables above (all except `SBX_SERVER_CONFIG` and
`SBX_GIT_SHA`); it must stay `0600` (`sbx server check` fails otherwise).

Generate keys with `openssl rand -base64 32` for a vault key and
`openssl rand -hex 32` for the master key.

## Health

`GET /healthz` returns `{"ok": true, "git_sha": ...}`. `git_sha` is the commit the
container image was built from (the `SBX_GIT_SHA` build argument) and is `null`
when the control plane runs from a source checkout. `GET /readyz` returns `503`
when the database cannot be read.

## Client variables

`SBX_BASE_URL`, `SBX_API_KEY` and `SBX_WORKSPACE_ID` configure the CLI and SDK
only; see [CLI](/reference/cli/).

## Running in production

Run behind HTTPS and set `SBX_COOKIE_SECURE=1`. `sbx serve` does not serve the
Console; host the built `console/` bundle and route `/api` to the control plane,
and set `SBX_PUBLIC_URL` and `SBX_ALLOWED_ORIGINS` to the Console's origin.
Back up PostgreSQL and the vault keys separately: the database alone cannot
decrypt credentials, and the keys alone hold no data.
