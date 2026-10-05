# Persistence conventions

* PostgreSQL is the only business authority (`control/persistence/migrations/*.sql`, applied in
  order under an advisory lock by `Database.migrate`).
* Typed columns carry every authorization, constraint and gate field; JSONB holds only versioned
  payload/spec/capability/result extensions.
* Owned rows carry `workspace_id`; foreign keys between owned rows are composite
  `(workspace_id, id)` so cross-workspace references are impossible.
* Enforced in the database:
  * append-only `session_events` and `audit_records` (UPDATE/DELETE rejected by trigger);
  * immutable `project_versions`; immutable accepted user Message content;
  * terminal-state guards on `turns`, `executions`, `executor_leases`, `snapshots`, `jobs`;
  * one active Turn per Session, one live Execution per Turn, one live lease per Session, one
    active Worktree barrier, one active capacity slot per `(connection, ordinal)`;
  * runtime source dedupe `UNIQUE (executor_lease_id, runtime_epoch, local_seq)`;
  * snapshot owner XOR (`environment` ↔ ProjectVersion, `checkpoint` ↔ Worktree);
  * Jobs have exactly one typed target column matching `target_family`.
* Tests run against a real throwaway PostgreSQL cluster (`tests/support/postgres.py`), never a
  mocked single-thread mapping.
