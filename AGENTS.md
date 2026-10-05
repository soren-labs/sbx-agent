# Agent instructions — unified rewrite

The sole architecture is `docs/architecture/unified/` at RFC base bf8cbe065da2f4c6bbca3781b4dce90cc31d39d3. Active contracts are `docs/specs/unified/`; `docs/archive/` is historical, not implementation authority.

Session identity, replaceable ExecutorLease and official CLI Harness stay separate. PostgreSQL owns typed projections plus committed append-only events. Application commands commit intent/Jobs; shared fenced claims execute effects. Reads never start or settle work. No Task/Agent/Run/V1/V2/hosted facade, duplicate store or model loop may be reintroduced.

Run `make lint`, `make spec-check`, `make test`, Console typecheck/test/build and relevant fault/owner isolation tests. Cloud-free tests use an isolated HOME/XDG and stripped credential environment. PostgreSQL tests require an explicitly disposable `SBX_TEST_PG_DSN`. No cloud connector is called by ordinary tests.

Never write or print real credentials/tokens/passwords in code, fixtures, logs, responses, comments or artifacts. Secret fixture fields use `REDACTED`. Only selected owner credentials reach the relevant effect; no ambient fallback. Do not inspect real credential files, use another worktree, alter host services or mutate production data without explicit task authorization. Manual credential disconnect must retain teardown authority or reject while dependent resources remain unconfirmed.

Benchmark PRs are stacked and must never be merged by the implementation agent. Do not inspect competing implementation tracks. Current benchmark authorization covers the full repository rewrite, scoped real acceptance and the disposable soren-labs/sbx-e2e-test repository only.
