# GPT unified architecture implementation

Frozen input: RFC #167, bf8cbe065da2f4c6bbca3781b4dce90cc31d39d3. All 13
canonical documents were read before editing. No competing implementation is
an input. This stack uses a fresh disposable PostgreSQL cohort and does not
migrate production or merge any PR.

## Phase 1

Implemented RFC 02 identity/ownership/Turn transitions, 04 typed PostgreSQL
schema, synchronous journal/UoW, command receipts, claims, resource fences and
notification outbox, and the foundational boundaries in 09/10. Existing
writers are untouched and cannot access new resource tables. Production
UUID7 is a recommendation; this Python 3.12 implementation uses UUID4 suffixes.

Actual PostgreSQL tests cover accepted intent replay after reconstruction,
changed-body rejection, parallel ordinals, concurrent SKIP LOCKED claims,
expired claim takeover with stable effect identity, stale resource fences,
projection/journal/outbox rollback, journal and terminal immutability,
owner denial, lifecycle reducer equivalence and pure reads. Runtime effects,
credential handling and outbox consumers are implemented in subsequent phases.

Each following phase replaces one boundary and records test/live evidence.
The final report will enumerate every acceptance gate, including failures and
explicitly deferred later extensions. No gate is inferred from table existence.

## Phase 2

Implemented RFC 03 runtime/Executor/Harness boundaries and 04 evidence ingestion:
SQLite operation journal/spool, explicit grants/fences, bounded CLI process-group
supervision, provider-native OpenCode normalization, truthful capability catalog,
portable file/native checkpoints, Modal tagged allocation and Local daemon boot.
Runtime loss seals interruption and quarantines compute, preserving Session and
Worktree identities. Typed execution admission and terminalization use the shared
Job claims/UoW; cancellation cannot lose to late success after committed intent.

Validation: full isolated make test 3528 passed / 11 skipped (191.75s), make lint
PASS; the final focused fault suite additionally exercises the subsequently
added runtime-loss test. Live Modal/OpenCode verification is reserved for phase 6.
Other provider lanes remain explicitly disabled. HTTP over Modal's TLS tunnel
is the tested protocol transport; WS enrollment exists without being production
transport evidence. Process-memory/PTY restore is explicitly unsupported.

Phase 1 CI's image-build errors were Docker registry DNS timeouts. Phase 2 opts
legacy networked image builds out of core CI, retaining real PostgreSQL checks.
