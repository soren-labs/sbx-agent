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
