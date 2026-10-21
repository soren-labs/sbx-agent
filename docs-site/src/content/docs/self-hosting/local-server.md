---
title: Run a local control plane
description: Set up, check, start, stop and diagnose your own SBX control plane with the sbx server commands.
---

`sbx server` runs the same control plane as `sbx serve` (API and Job workers in one
process) in the background, with a private configuration and, if you want, a private
PostgreSQL cluster. Nothing is sent to an SBX-operated service.

## Requirements

- A source checkout with `uv sync` (Python 3.12+).
- PostgreSQL 15 or newer. Either the server binaries are installed (`initdb`,
  `pg_ctl`: Ubuntu `sudo apt install postgresql`, macOS `brew install postgresql@17`)
  and SBX creates a private cluster, or you bring a database you already run.
- Linux for the `local` executor (it supervises processes with `/proc` and `prctl`).
  On other systems use the `modal` executor.

## First run

```bash
uv run sbx server init      # private config + private PostgreSQL + fresh keys
uv run sbx server check     # configuration, PostgreSQL and executor checks
uv run sbx server start     # starts in the background, waits until /readyz is ok
```

`init` writes `$XDG_CONFIG_HOME/sbx/server.env` (default `~/.config/sbx/server.env`,
directory `0700`, file `0600`). It holds a newly generated vault keyring
(`SBX_VAULT_KEYS`) and runtime master key (`SBX_RUNTIME_MASTER_KEY`), the database
URL and the listen address. Instance data lives in `$XDG_DATA_HOME/sbx` (default
`~/.local/share/sbx`): blobs, local executor state, the mail outbox, the server log
(`log/server.log`, `0600`) and, by default, the PostgreSQL cluster.

The private cluster listens only on a Unix socket inside a `0700` directory, accepts
only your own OS user (`peer` authentication) and rejects TCP. It is ordinary
PostgreSQL: back it up like any other database.

| Option | Effect |
| --- | --- |
| `--config PATH` | Use another config file (also `SBX_SERVER_CONFIG`); every `sbx server` command takes it. |
| `--data-dir DIR` | Instance data directory. |
| `--external-database` | Use your own PostgreSQL. Its connection URL is read from stdin, never from the command line: `sbx server init --external-database < dsn.txt`. |
| `--host`, `--port` | Listen address (default `127.0.0.1:8800`). |
| `--public-url` | Base URL for email links (default: the API URL). |
| `--executors` | Executor backends (default `local,modal`). |

`init` never replaces an existing config file, and refuses a data directory or
database that already holds SBX data: new keys could not decrypt the credentials
stored there. Use the config that created it, or choose an empty location.

Back up the config file separately from the database. Without `SBX_VAULT_KEYS`
stored credentials cannot be decrypted; the keys alone hold no data.

## Create the first account

Without `SBX_RESEND_API_KEY` email goes to the local outbox. From the CLI:

```bash
export SBX_BASE_URL=http://127.0.0.1:8800
uv run sbx auth register --email you@example.com   # password from the prompt or stdin
uv run sbx server outbox --to you@example.com      # shows the verification link
uv run sbx auth verify                             # paste the token= value of the link
uv run sbx auth login --email you@example.com      # stores an API key (0600)
uv run sbx status
```

Or start the [Console](/guides/console/) and register there.

## Everyday commands

| Command | Effect |
| --- | --- |
| `sbx server start [--timeout S]` | Start the managed PostgreSQL (if any) and the control plane; wait until it is ready. Starting a running instance reports `already_running`. |
| `sbx server stop [--keep-postgres]` | Stop the control plane (SIGTERM, then a bounded wait) and the managed PostgreSQL. |
| `sbx server restart` | Stop and start again; the managed PostgreSQL keeps running. |
| `sbx server status` | Process, `/healthz` and `/readyz` of this instance. |
| `sbx server check` | Read-only configuration, PostgreSQL, migration, executor and port checks. |
| `sbx server logs [--lines N]` | The end of `log/server.log`. |
| `sbx server outbox [--to EMAIL]` | The newest message in the local mail outbox. |
| `sbx serve` / `sbx migrate` | Foreground server or migrations; they also read the config file. |

Start, stop and restart of one config are serialized, and they only act on the
process that config started (checked by PID, start time, data directory and config
path). A second config pointed at the same data directory is refused while the first
one runs.

All durable state is in PostgreSQL. A stop or restart keeps Sessions, Turns, Messages,
Connections and the event journal; Jobs claimed by the stopped workers are reclaimed
after their claim expires. Local executor runtimes are durable leases: `stop` reports
them as `local_runtimes_running`, the next start adopts them, and idle ones are
released after the idle timeout while the control plane runs.

## Diagnostics and exit codes

`sbx server check`, `start` and `status` (on the server host) and `sbx status` (any
client) print JSON with a `problem` and a list of `checks`. Each finding has one
category, and the exit code names the first failing one:

| Exit | Category | Meaning |
| --- | --- | --- |
| `0` | | Everything needed is in place. |
| `3` | `config` | A setting is missing or malformed, the config file is readable by others, the port is taken, or migrations do not match. |
| `4` | `reachability` | The control plane or PostgreSQL does not answer, or `/readyz` fails. |
| `5` | `authentication` | PostgreSQL refused the configured identity (server checks), or the API key is missing or rejected (`sbx status`). |
| `6` | `executor` | No executor backend can run Turns: for example only `modal` is enabled and the workspace has no ready Modal Connection, or the `local` executor is unavailable on this host. |

Executor problems never block `start`: the API stays usable and Turns report
`executor_unavailable` or a failed launch. `check` also lists which official CLIs the
`local` executor finds on `PATH`; a Turn for a Harness whose CLI is missing fails with
`spawn_failed`.

`sbx status` reads `/healthz`, `/readyz`, `/api/me`, `/api/executor-backends` and
the workspace's Connections; it never creates anything. The SDK equivalent is
`client.diagnose()`.

## Configuration precedence

Environment variables override the config file, and `check` reports where each
setting came from (`environment`, `file` or `default`) without printing secret
values. The control-plane variables are listed in
[Configuration](/self-hosting/configuration/).
