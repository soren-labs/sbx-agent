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

## Phase 3

Added email/password identity with hashed sessions/CSRF/one-use verification and
login rate limits, immutable ProjectVersions and effective inputs, encrypted
manual Connection/CredentialVersion lifecycle, version-pinned validation Jobs,
Zen model metadata/free preference, GitHub per-effect access, Modal owner client,
private Git clone askpass, durable provider slots/Workspace compute quota,
checkpoint Jobs and verified exact-lease teardown. Native credential affinity
refuses unverified cross-account continuation. No ambient provider fallback.

Validation: isolated make lint PASS; full suite 3536 passed / 11 skipped / 8
warnings (162.04s); focused tests also cover ciphertext AAD/tamper/keyring,
credential replacement/revoke/stale validation, CSRF, owner denial, immutable
Project pinning and concurrent last-slot admission. An earlier full run had a
legacy wall-clock ACK test exceed 1s by 30ms under load; rerun and full suite pass.

Known limits: catalog access is not inference proof (live gate remains phase 6).
Email transport is an injected private operator concern, not a fifth user
credential. Benchmark verified-user provisioning is not public registration
verification evidence. Environment cache reuse is deliberately disabled until
secret-safe cache evidence exists; private Session checkpoints take precedence.
