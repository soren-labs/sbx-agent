# Authority and transactions

Implements RFC 02 "Authority and mutation ownership" and RFC 04 "Durable Job and claim protocol".

## One writer per resource

| State | Only writer (module) | Path |
| --- | --- | --- |
| Session / Message / Turn | `control/application/sessions.py` | commands; `finish_turn` is the single terminalization path |
| Committed journal | `UnitOfWork.append_event` | sequence allocated by locking the Session row (`next_event_seq`) |
| Jobs | `control/jobs/claims.py` | `claim_next` / `finish` compare id + holder + generation + expiry |
| Command replay | `UnitOfWork.dedupe_begin/finish` | `(principal, workspace, command_kind, key)` + request digest |

Later phases add the Execution, Connection, Project, ChangeSet, Delivery and Delegation
applications; each is the sole writer of its tables. Workers never mutate state directly: handlers
call application commands inside `JobContext.commit`, which asserts the claim is still current in
the same transaction (lock order: domain rows first, Job row last).

## Transactions

* Writes: `Database.run(fn)` — one transaction, retried on deadlock/serialization failure.
  Rolled-back attempts commit nothing (no sequence/ordinal consumption).
* Reads: `Database.read(fn)` — `REPEATABLE READ READ ONLY`. Any write inside a query raises at the
  database, enforcing "reads never settle or launch work" (A10). `event_watermark` is read in the
  same snapshot as the entity state.
* No network/process calls happen inside a transaction.

## Dedupe windows

* Command idempotency records persist for **7 days** (`DEDUPE_TTL`). Same key + same body returns
  the committed response; same key + different body returns `idempotency_conflict`.
* Resource constraints (unique triggering Message, unique allocation/operation IDs, partial unique
  active rows) still prevent duplicate effects after the window expires.
* Job dedupe: unique active `(kind, dedupe_key)`; terminal Jobs free the key for a new intent.

## Job claim protocol

States `queued → claimed → {queued (Continue), retry_wait, succeeded, failed, cancelled}`. Claims
use `FOR UPDATE SKIP LOCKED`, increment `claim_generation` and insert a `job_attempts` row.
Expired claims are reclaimed with a higher generation (`reclaimed=true`); the stale holder's
completion raises `StaleClaim` even if its external call succeeded, and the successor inspects the
same `effect_id`. Retries use bounded exponential backoff with jitter and honor `retry_after`,
`max_attempts` and `deadline_at`. Waiting (capacity, children, polling) releases the claim with
`Continue`; no worker sleeps holding a transaction.
