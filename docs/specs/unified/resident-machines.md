# Resident machines

A resident machine is a Linux workstation or VPS that the operator of a self-hosted control plane
registers for one workspace. Sessions on it use executor backend `resident`. Code:
`control/executors/resident.py` (SSH transport, tunnels, backend), `runtime/resident/agent.py`
(the program run on the machine), `control/application/resident.py` (discovery and operator
registration), `control/selfhost/machines.py` (`sbx server machine`). Migration
`0009_resident_machines.sql`.

## Trust and tenant boundary

* The machine and its SSH account belong to the operator. Runtimes run as that Unix user.
  Each lease gets its own daemon, worktree, `HOME` and state under
  `<state_root>/leases/<lease_id>/` (mode `0700`); the daemon environment is an allow-list
  (`PATH`, `HOME`, `LANG`, `PYTHONPATH`, the `SBX_*` boot variables and operator-pinned
  `*_BIN` CLI overrides). The SSH user's `HOME`, shell environment and developer CLI logins
  (`~/.claude`, `~/.codex`, ...) are not passed to runtimes.
* This is separation, not a multi-tenant sandbox (`capabilities.multi_tenant = false`): code in
  one lease can reach files the SSH user can reach. Register a machine only for a workspace you
  trust with that account; prefer a dedicated Unix user.
* Optional hardening (not exercised by the B2 acceptance): a dedicated key whose
  `authorized_keys` line is
  `restrict,port-forwarding,permitopen="127.0.0.1:*",command="cd /srv/sbx && exec /srv/sbx/.venv/bin/python -E -s -m runtime.resident.agent --state-root /srv/sbx-state --forced" ssh-ed25519 …`
  (`restrict` alone would also forbid the runtime forward). With `--forced` the agent reads its
  verb from `SSH_ORIGINAL_COMMAND` and refuses anything but its four verbs.

## Registration (operator only)

`sbx server machine add NAME (--workspace ID | --owner-email EMAIL) --host --port --user
--identity-file --known-hosts-file --remote-python --remote-root [--state-root] [--remote-path]
[--cli-override NAME=/abs/path]... [--max-leases 4] [--connect-timeout 10] [--no-probe]`
writes a `resident_machines` row with the server config's database. It refuses: a relative path,
a host/user that is not a plain name, an identity file readable by group/others, a known_hosts
file without an entry for `host` / `[host]:port` (no trust on first use), a duplicate live name
in the workspace, and (unless `--no-probe`) a target whose probe is not `reachable`.
`machine list`, `machine probe NAME|ID`, `machine remove NAME|ID` (refused while any lease on the
machine is live or quarantined) complete the set. Output is JSON; exit codes follow
[self-hosting](self-hosting.md): 3 config / runtime missing or mismatched, 4 unreachable,
5 host key or authentication.

`target` (host, port, user, key and known_hosts paths, remote Python and checkout, state root,
PATH, CLI overrides, connect timeout) is stored server-side and is never returned by the API and
cannot be set through it. The private key stays in the operator's file.

## SSH transport

Every call is `ssh -F none -T -o BatchMode=yes -o StrictHostKeyChecking=yes
-o UserKnownHostsFile=<file> -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no
-o IdentitiesOnly=yes -i <key> -o ForwardAgent=no -o ForwardX11=no -o RequestTTY=no
-o ConnectTimeout=<n> -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -p <port> -l <user> <host>
-- cd -- <remote_root> && exec <remote_python> -E -s -m runtime.resident.agent [--state-root R] <verb>`
with one JSON request on stdin and one JSON reply on stdout. ssh gets only `PATH`, `LANG`, `HOME`,
`USER`, `LOGNAME` and `SSH_AUTH_SOCK` from the server environment (no `SBX_*` secrets). Failures
are classified from exit 255 + stderr: `host_key_unverified`, `auth_failed`, `unreachable`; a
non-JSON reply otherwise is `runtime_missing`.

Runtime frames use protocol v1 unchanged (HMAC lease grants) through an SSH local forward
`127.0.0.1:<free>` → machine `127.0.0.1:<daemon port>` (`ExitOnForwardFailure`,
`ServerAliveInterval=5`). The lease endpoint is stored as `resident://<machine>/<lease>`; the
runtime connector resolves it on every channel and re-creates a dead forward, so a dropped link or
a restarted control plane reaches the same daemon. Forwards close when the lease is terminated or
the control plane exits.

## Agent verbs

| Verb | Request | Reply |
| --- | --- | --- |
| `probe` | `path`, `cli_overrides` | `runtime_digest`, `python`, `boot_id`, `harnesses` (CLI found on PATH), `live_leases` |
| `allocate` | `operation_id`, `lease_id`, `generation`, `enrollment_key`, `expected_digest`, `path`, `cli_overrides` | `handle` |
| `lookup` | `operation_id` | `found`, `handle` |
| `terminate` | `operation_id` | `stopped`, `status` |

Identifiers must match `^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$`. The enrollment key travels only on
stdin; replies and errors never echo request content.

* **Version.** The digest covers `protocol/`, `runtime/{daemon,harnesses,resident,security,
  subscriptions}` sources. `allocate` refuses `runtime_mismatch` when it differs from the control
  plane's own digest (→ `runtime_incompatible`, not retryable); `probe` reports
  `runtime_mismatch`. `hello.image_digest` is `resident:<digest>`.
* **Idempotent allocation.** Under a per-operation `flock`, the agent writes an intent record,
  starts the daemon (`setsid`, stdio to `daemon.log`, SIGHUP ignored by the agent), records pid,
  kernel start time and boot id, then waits for the port file. A retry, a lost SSH response or an
  interrupted agent adopts the same daemon (found by record, or by its lease state dir in the
  command line) instead of starting another. A failed start is stopped and its record removed only
  after the stop is confirmed.
* **Ownership.** A process is the allocation's daemon only if pid, start time and boot id match
  the record and its command line names the lease's state dir. `lookup` answers `terminated`
  otherwise, and `found: false` only for an operation that has no record (authoritative: the
  lookup holds the operation lock).
* **Terminate.** Targets are the verified daemon and its descendants; when the daemon is gone,
  processes of this boot that started after it and whose cwd or arguments lie in the lease's
  `work/` or `state/`. Each target is opened as a pidfd and re-verified before SIGTERM, then
  SIGKILL after 5 s; `stopped` only when every pidfd reports exit. A bare or recycled pid is
  never signalled. Lease directories are kept for the operator (no automatic pruning).
* The daemon survives the SSH session that started it as long as logind does not kill user
  processes on logout (`KillUserProcesses=no`, the Ubuntu default).

## Execution semantics

* `executor.resident_machine_id` is required with backend `resident`, must name an `active`
  machine of the Session's workspace (other workspaces: `not_found`), and is invalid with other
  backends. It is pinned on the Session and recorded on every lease.
* Capacity: a machine is occupied by every lease whose runtime may still run there — live leases
  and lost leases still `quarantined` (termination unconfirmed). At `max_leases` new leases wait
  (`waiting_capacity`); the API's `capacity.live_leases` uses the same count and
  `capacity.unconfirmed_leases` shows the quarantined part.
* SSH failures in `lookup`/`describe`/`terminate`/`allocate` raise retryable
  `executor_unavailable`; they never mean absent, terminated or succeeded.
* Reconcile on a resident lease tolerates an unreachable runtime for
  `unreachable_grace_seconds` (900 s, below the daemon's 1800 s lease authority). After
  `unreachable_threshold` failures the lease's `observed_status` becomes `unreachable` and
  `executor.unreachable {lease_id, since}` is appended; the Turn stays in its state. When the
  runtime answers again the same epoch continues, `observed_status` returns to `ready` and
  `executor.reachable {lease_id}` is appended. After the grace window the existing rule applies:
  execution `unknown`, Turn `interrupted`/`failed` with `outcome_unknown`, lease `lost` and
  quarantined until termination is confirmed.
* A machine that cannot be reached at allocation keeps the Turn `preparing` while `turn.dispatch`
  retries with backoff (20 attempts), then fails it with `executor_unavailable`; the user can
  cancel at any time. Nothing is reported as succeeded.
* Live files/terminals on an unreachable machine answer `executor_unavailable` (reads) or
  `outcome_unknown` (writes; retry with the same Idempotency-Key).

## API

| Route | Notes |
| --- | --- |
| `GET /api/workspaces/{id}/resident-machines?probe=true` | live machines of the workspace |
| `GET /api/resident-machines/{id}?probe=true` | one machine; other workspaces get 404 |
| `GET /api/sessions/{id}/executor` | adds `resident_machine {id,name,state,reachability}` and per-lease `observed_status`, `runtime_epoch` |
| `GET /api/executor-backends` | lists `resident` when enabled |

Machine view: `{id, workspace_id, name, state, executor_backend: "resident", capacity
{max_leases, live_leases, unconfirmed_leases}, reachability {status, reason, checked_at, runtime
{digest, expected_digest, digest_match, python, harnesses, live_runtimes}}, created_at}`. `status`
is `reachable`, `unreachable`, `host_key_unverified`, `auth_failed`, `runtime_missing`,
`runtime_mismatch` or `unchecked` (`probe=false`, machine disabled, or executor not enabled). The
probe is a bounded read-only SSH call cached for 15 s in memory; discovery writes nothing.
Session resources carry `executor.resident_machine_id` and `executor.lease_observed_status`.

SDK: `client.resident_machines.list(probe=)` / `.get(id, probe=)`, `client.files`,
`client.terminals`, `sessions.create(idempotency_key=…)`. CLI: `sbx machines list|show`,
`sbx sessions create --machine ID|NAME`, `sbx sessions executor|activate|release`.

## Not covered

macOS or other non-Linux machines (the agent needs `/proc` and pidfds), installing SBX on the
machine, automatic lease-directory pruning, subscription logins on resident machines (B3), and the
Console UI.
