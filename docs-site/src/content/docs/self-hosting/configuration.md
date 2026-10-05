---
title: Configuration
description: Every environment variable of the SBX control plane, with defaults and how to run it.
---

The control plane reads its configuration from the environment at start-up.

```bash
sbx serve --host 127.0.0.1 --port 8800        # API + workers in one process
python -m control.composition serve            # same thing
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

The process exits at start-up if any of these is missing.

## Optional

| Variable | Default | Meaning |
| --- | --- | --- |
| `SBX_DATA_DIR` | `./.sbx-data` | Local blobs, git work area, local executor state and the mail sink. |
| `SBX_PUBLIC_URL` | `http://localhost:8800` | Base URL used in verification and reset links. |
| `SBX_ALLOWED_ORIGINS` | the public URL | Comma-separated origins allowed for cookie mutations. |
| `SBX_COOKIE_SECURE` | `0` | Set to `1` to mark cookies `Secure` (use behind HTTPS). |
| `SBX_EXECUTORS` | `local,modal` | Enabled executor backends. |
| `SBX_RESEND_API_KEY` | unset | Send email through Resend. Without it, mail is written to `$SBX_DATA_DIR/mail/` (directory `0700`, files `0600`). |
| `SBX_WORKER_THREADS` | `4` | In-process job worker threads. |

Generate keys with `openssl rand -base64 32` for a vault key and
`openssl rand -hex 32` for the master key.

## Health

`GET /healthz` returns `{"ok": true}`. `GET /readyz` returns `503` when the
database cannot be read.

## Client variables

`SBX_BASE_URL`, `SBX_API_KEY` and `SBX_WORKSPACE_ID` configure the CLI and SDK
only; see [CLI](/reference/cli/).

## Running in production

Run behind HTTPS and set `SBX_COOKIE_SECURE=1`. `sbx serve` does not serve the
Console; host the built `console/` bundle and route `/api` to the control plane,
and set `SBX_PUBLIC_URL` and `SBX_ALLOWED_ORIGINS` to the Console's origin.
Back up PostgreSQL and the vault keys separately: the database alone cannot
decrypt credentials, and the keys alone hold no data.
