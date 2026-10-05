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
