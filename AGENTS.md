# AGENTS.md

This repository implements the **unified sbx-browser architecture** defined
by RFC 167 — see `docs/architecture/unified/` (normative). One architecture;
there is no legacy Task/Agent/Run/V1/V2/hosted machinery.

## Invariants (RFC 167)

- Session = durable identity; sandbox/native CLI/PR are never Session
  identity. ExecutorLease is replaceable; Worktree identity survives compute
  loss.
- `sbx-runtime` (`runtime/daemon` + `protocol/`) is supervised
  infrastructure, not a proprietary agent/model loop. Harnesses
  (`runtime/harnesses/`) are thin adapters over official provider CLIs.
- Authoritative state = typed relational projections (`control/persistence`)
  plus the committed append-only `session_events` journal.
- All slow/recoverable effects go through durable Jobs with
  claims/fences/outbox (`control/jobs`). Reads do not settle or launch work.
- ChangeSet is immutable with a canonical `subject_digest`; Delivery owns
  exact-subject Git effects; Delegation is a child Session with a typed
  ResultContract.
- One Connection model covers Modal/GitHub/OpenCode-Zen; secrets live only
  in encrypted CredentialVersion records (`control/security/vault`).
- One business API (`control/api`, mounted at `/api`) and one Console state
  model (`console/src/unified/`); the SDK is `src/sbx/sdk/unified.py`.

## Rules

1. Before committing: `make lint` and `make test` must be green without any
   cloud credentials.
2. Never write credentials, tokens or secrets into code, fixtures, logs,
   events, PRs or issues. Fixture token fields use `REDACTED`.
3. `import modal` is allowed only inside `control/executors/modal.py` and
   `runtime/image.py`; tests must not instantiate real cloud clients.
4. Tests use isolated HOME/XDG and a disposable Postgres database
   (`SBX_TEST_DATABASE_URL`); production data is never touched.
5. The runtime image is version-pinned via `runtime/packages.txt`; run
   `python -m runtime.image --write-dockerfile` after changing pins.
6. Real-network acceptance lives in `tests/mvp/` (`make test-mvp`); it
   requires real credentials and never prints secret values.
