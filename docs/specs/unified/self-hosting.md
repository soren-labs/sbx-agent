# Self-hosted control plane (`sbx server`)

Implements the B1 self-hosting baseline. The server is the one composition root
(`python -m control.composition serve`); `control/selfhost/` adds only operator tooling around
it. PostgreSQL stays the business authority; identity, API keys and the vault are unchanged.

## Configuration

* **File**: `$XDG_CONFIG_HOME/sbx/server.env`, or `--config PATH` / `SBX_SERVER_CONFIG`.
  `KEY=VALUE` lines limited to `control.selfhost.config.SERVER_KEYS` (the control-plane variables
  plus `SBX_LISTEN_HOST`, `SBX_LISTEN_PORT`, `SBX_MANAGED_POSTGRES`); other keys are refused.
  Created with `O_EXCL`, mode `0600`, directory `0700`; never overwritten. `check` fails when the
  file is group/world accessible.
* **Precedence**: process environment > file > defaults. With a file, `SBX_DATA_DIR` defaults to
  `$XDG_DATA_HOME/sbx`; without one, to `./.sbx-data` (unchanged bare-process behaviour).
  `describe()` reports `source` per key and prints secret keys only as `(set)`.
* **Child environment**: the started process gets the parent environment without any
  `SERVER_KEYS`/`SBX_SERVER_CONFIG`, then exactly the effective values and
  `SBX_SERVER_CONFIG=<selected file>`. A `--config` instance never inherits another config's
  optional settings.
* **Validation** (`config.problems`, shared with `UnifiedConfig.from_env`): required keys present;
  each vault key base64 of 32 bytes; master key hex ≥ 32 bytes; executors ⊆ {`local`, `modal`, `resident`};
  positive worker threads; valid port; `SBX_MAIL_FROM` with `SBX_RESEND_API_KEY`. `from_env`
  exits with the key name and fix, never the value.

## `init`

Generates a fresh vault keyring (`k1`, 32 random bytes) and runtime master key (32 random bytes)
and writes them only to the config file; the JSON result never contains them. Without
`--external-database` it creates a private cluster in `$SBX_DATA_DIR/postgres` from the newest
installed server binaries: `initdb -U <os user> --auth-local=peer --auth-host=reject`,
`listen_addresses=''`, socket directory `0700` with `unix_socket_permissions = 0700`, database
`sbx`. The socket path must fit `sun_path` (≤ 103 bytes). `--external-database` reads the URL
from stdin. Refusals (exit 3): config file exists; data directory already holds instance data
(`postgres`, `blobs`, `executor`, `git`, `mail`, `run`); external database already has
`schema_migrations`. Fresh keys are never paired with data sealed by other keys.

## Lifecycle

* **Files**: `$SBX_DATA_DIR/run/server.pid` (`PID STARTTIME`, `0600`), `run/server.lock`,
  `log/server.log` (`0600`, append), `log/postgres.log`.
* **Serialization**: `start`, `stop`, `restart` hold an exclusive `flock` on `run/server.lock`.
* **Identity**: a PID belongs to this instance only if alive, not a zombie, same kernel start
  time as recorded, `control.composition` in its cmdline, and `SBX_DATA_DIR` and
  `SBX_SERVER_CONFIG` of this instance in `/proc/PID/environ` (liveness only where `/proc` is
  absent). A live control plane of another config over the same data directory is an
  *occupant*: `start` refuses (`data_dir_in_use`, exit 3) and `stop` leaves its PID file and
  managed PostgreSQL alone.
* **start**: already running → `already_running`. Otherwise config checks, managed PostgreSQL
  start, database checks, port check; any non-executor failure → `refused` with its exit code and
  no process. Executor failures never block start. Spawns detached (`start_new_session`), writes
  the PID file, waits for `/readyz` 200; early exit → `failed` with the log tail (exit 3);
  timeout → terminated, `failed` (exit 4).
* **stop**: SIGTERM, bounded wait, SIGKILL; PID file removed; managed PostgreSQL stopped with
  `pg_ctl -m fast` unless `--keep-postgres`. A `pg_ctl` failure with the cluster still running is
  reported (`ok: false`, exit 4), never as stopped. Live local-executor runtimes are reported as
  `local_runtimes_running`; they are durable leases adopted by the next start.
* **restart**: stop (PostgreSQL kept) then start under one lock.
* **status**: process, `/healthz`, `/readyz` and managed PostgreSQL state as checks.

Durable state (Sessions, Turns, Messages, Connections, journal, Jobs) is in PostgreSQL; claims
of stopped workers expire and are reclaimed per authority.md.

## Checks and exit codes

Each check is `{name, category, status: ok|warn|fail, message, action?}`; `problem` is the first
`fail` in category order `config`, `reachability`, `authentication`, `executor`; exit codes
3, 4, 5, 6 (0 when none fails). The SDK (`sbx.sdk.diagnostics`) uses the same categories and
codes for client-side `sbx status`.

| Check | Category on failure |
| --- | --- |
| config file presence/mode, required keys, key formats, data dir writable, listen port | config |
| managed cluster missing, PostgreSQL binaries missing | config |
| managed cluster stopped (warn), connection refused / timeout / no route | reachability |
| SQLSTATE `28000`/`28P01` or libpq auth text (password, peer, pg_hba, unknown role) | authentication |
| database does not exist (`3D000`), malformed DSN | config |
| PostgreSQL < 15, unknown applied migrations | reachability / config |
| local executor: non-Linux, runtime not importable, no git (warn); missing Harness CLIs (warn) | executor |
| modal executor: `modal` package missing | executor |
| resident executor: `ssh` client missing | executor |
| no usable executor | executor |

Database error messages keep only the first libpq line with the DSN replaced; DSNs and secrets
are never printed.

## Resident machines

`sbx server machine add|list|probe|remove` registers Linux machines a workspace may run Sessions
on (executor `resident`); see [resident-machines.md](resident-machines.md).
