# sbx-runtime protocol v1

Implements RFC 03. Wire types: `protocol/runtime.py`; daemon: `runtime/daemon/`; control client:
`control/runtime_client/`.

## Transport (documented deviation)

The RFC names an executor-initiated WebSocket at `/internal/runtime/connect`. This implementation
carries the **same frames** as authenticated JSON over HTTPS request/response, with the control
plane as client: Modal exposes the daemon through an encrypted tunnel, and Local uses loopback.
Evidence flow is pull-based (`/rt/events`, then `/rt/ack` after the DB commit). Operation identity,
fences, dedupe, the committed-ack rule and epoch semantics are unchanged. An outbound WebSocket can
be added later without changing frames.

| Route | Scope | Purpose |
| --- | --- | --- |
| `GET /healthz` | none | liveness only, no data |
| `POST /rt/hello` | read | lease id/generation, runtime epoch, protocol `{major,minor}`, image digest, Harness manifests, recovered operations, spool watermarks, health |
| `POST /rt/op` | manage | mutating frame (below) |
| `POST /rt/query` | read | `operation.status`, `files.list`, `files.read`, `changes.observe`, `health.report` |
| `POST /rt/events` | read | spool records after a local sequence, plus the runtime epoch |
| `POST /rt/ack` | read | advance the acknowledged watermark (only after control committed) |

## Grants and fences

* Per-lease key = `HMAC(master, "sbx-lease:{lease}:{generation}")`. Only the derived key enters the
  executor; the control plane re-derives it after a restart, so no lease secret is stored in the database.
* Grant = HMAC-signed `{lease_id, generation, scope, exp}`, 5-minute TTL by default. A wrong lease
  is rejected with 403; a stale generation with 409 `stale_fence`; an expired grant with 401.
* Every authenticated request extends lease authority. When authority expires, the watchdog stops
  active CLI processes and refuses new Turns.

## Operation frame

`{operation_id, operation_kind, session_id, lease_id, lease_generation, schema_version,
request_digest, payload, secrets}`. `request_digest = sha256(canonical({kind, payload}))` and
excludes `secrets`. The same ID with the same body returns the durable status (`replayed: true`);
the same ID with a different body returns `operation_conflict`.

Kinds implemented: `worktree.restore`, `turn.start`, `turn.cancel`, `files.write`, `check.run`
(declared argv only), `snapshot.prepare`, `lease.renew`, `runtime.shutdown`. `changes.capture` and
`changes.apply` are added in the ChangeSet phase. Generic exec does not exist.

## Executor boot environment

The Executor starts the daemon with `python -m runtime.daemon.main --state-dir … --work-dir …
--port …`. It sets only the variables below, which operators do not configure:

| Variable | Meaning |
| --- | --- |
| `SBX_RUNTIME_KEY` | hex per-lease key (required); removed from the daemon's environment at startup |
| `SBX_LEASE_ID` / `SBX_LEASE_GENERATION` | the lease identity and generation the daemon accepts |
| `SBX_IMAGE_DIGEST` | image identity reported in `hello` |
| `SBX_LEASE_TTL` | optional lease-authority window in seconds (default 1800) |
| `SBX_SPOOL_MAX_UNACKED` | optional spool bound (default 20000) |
| `OPENCODE_BIN` / `CODEX_BIN` / `CLAUDE_BIN` / `GROK_BIN` / `COMMANDCODE_BIN` | optional CLI command overrides (tests point them at fakes) |

**Modal resources.** The Modal Executor runs inside the **owner's** Modal workspace, using the
selected Modal Connection's token. On first use it creates the app `sbx-executor`
(`create_if_missing`). It builds one image per Connection and runtime-recipe digest: Debian slim
with Python 3.12, Node 22, `opencode-ai@1.18.34` and the `runtime`/`protocol` sources. Sandboxes
carry the tags `sbx_alloc`, `sbx_lease`, `sbx_session` and `sbx_workspace`, have a 6-hour hard
timeout, and expose only the encrypted runtime port 8790. No Modal Secrets are attached. Image
builds and sandbox time are billed to that workspace.

**Allocation identity and release.** Each lease has a durable `allocation_operation_id` (the
effect identity). Before any backend create the lease records `observed_status=allocate_requested`;
Modal sandboxes are created with the unique name `sbx-<operation id>` *and* their tags in the same
call, so a lost create response, a retry or a lagging tag index rediscovers the sandbox (Modal
refuses a second running sandbox with that name) instead of creating a twin. The handle is persisted
before the runtime handshake; once persisted, retries only observe it, and a dead allocation loses
the lease (confirmed) rather than being replaced under the same identity. Lookup failures are never
treated as absence. The Local executor registers each daemon before its readiness wait, kills a
failed start, and adopts a registered daemon on retry (per-operation file lock).

`executor.release` first fences the lease (no new create can start), then confirms the stop: with a
handle it requires confirmed termination; without one it resolves the allocation by operation
identity, and absence is authoritative only after the in-flight create window
(`allocation_window_seconds`). An unconfirmed stop quarantines the lease (blocking new leases for
the Session) and a periodic sweep re-enqueues release until termination is confirmed.

## Journal and spool

SQLite WAL with `synchronous=FULL` in the protected state dir (outside the Worktree).

* Acceptance is journaled before spawn, and the pid right after spawn.
* After a restart, open operations are never relaunched. A live recorded pid is stopped, and an
  operation with no pid is reported as `launch: ambiguous`. Both end with
  `execution.observed_terminal{verdict: unknown}` and `execution.stopped{recovered: true}`.
* The runtime epoch survives restarts with the journal intact; an empty journal gets a new epoch.
* The spool is bounded (`SBX_SPOOL_MAX_UNACKED`). Under pressure the CLI is stopped and diagnosed;
  terminal evidence uses reserved headroom. New Turns are refused until evidence is acknowledged.
* Known secrets and secret-looking structured fields are redacted before spooling.

## Checkpoints

`snapshot.prepare` holds the exclusive barrier and requires a fully acknowledged spool. It returns
a tar.gz of the Worktree plus Harness-approved native state; credential files (`auth.json`) are
excluded and are scrubbed after every Turn. Control verifies the digest, stores the blob privately,
and seals a `checkpoint` Snapshot. `worktree.restore{checkpoint_b64}` rejects archives with
traversal or link escapes. Backend-native memory or filesystem snapshots are not used, and process,
PTY and socket state is never claimed as restored.
