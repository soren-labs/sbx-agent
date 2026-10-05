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

## Phase 4 — ChangeSet, Delivery, Delegation

Immutable manifest v1 records sorted path/type/mode/content digest, repository/base/head/tree and canonical SHA256 subject identity. Runtime captures binary/untracked/deleted/executable/symlink files, rejects excluded changed files and known credential material, and validates the entire staged application before swapping Worktrees. Durable capture barriers prevent a new Turn racing a capture. Ready ChangeSets cannot be mutated in PostgreSQL.

Delivery uses the user's explicit GitHub credential, deterministic Git database materialization, a new deterministic branch and a marked draft PR. Remote evidence is committed as append-only steps behind both Job and global repository/ref target claims. Lost responses adopt the same exact head/PR; a conflicting existing branch is rejected because REST ref PATCH lacks expected-old atomicity. Merge has a separate typed intent, freshly checks exact head/subject, independent validated results, required checks, draft/mergeability and provider head-CAS. Strict atomic base stability is explicitly unsupported and fails closed. Child Sessions cannot ship by default.

Delegation atomically creates a distinct child Session/Worktree and Turn, pins an immutable ChangeSet and typed platform-enforced ResultContract, and provides spawn/message/wait/result/cancel commands through the same application services. Successful results need real execution/native evidence; test passing results need observed successful tool commands. Wait registration and result publication serialize on the delegation; notification is an ordinary queued parent Message. No Review engine or workflow DAG exists.

Validation: canonical digest vector, binary/deleted/mode/symlink capture/apply, atomic application failure, exact-subject contract, stale remote head, independent review gate, immutable database seal, lost PR response reclaim, durable wait/child isolation and denied child shipping. Full lint/backend results recorded below after completion.

Limitations at this boundary: application tool gateway transport is wired in the product phase; export transport, generic file transfer and supervised product I/O are completed with that surface. Review is advisory evidence; native CLI structured output remains prompt-only plus strict platform validation. Git delivery materializes/pushes via GitHub Git Database API rather than invoking a git push process. This preserves pinned object/ref/PR semantics without exposing GitHub credentials to the coding process.

Phase 4 results: `make lint` passed; full isolated PostgreSQL/backend suite **3541 passed, 11 skipped** (163.86s); focused unified suite **30 passed**. Fixed a legacy CI-only test assumption that monotonic uptime already exceeds the listing rebuild interval (the new machine was younger than that interval); its focused suite **21 passed**. This legacy test is retired at final cutover.

## Phase 5 — Unified product surface

Added the single `/api` business router, resource schemas, generated OpenAPI/shared TypeScript schemas and CI drift check. Application creates can atomically accept the initial Message/Turn/Job. Cookie auth uses Secure/HttpOnly/SameSite login cookies and CSRF for mutations; password changes revoke login/API-key authority and scoped API keys are hashed with plaintext returned only once. Pydantic validation responses omit write-only inputs. Lists/events are bounded, owner-scoped pure projections. The Session snapshot includes Worktree, Messages, Turns, parts and watermark in one repeatable-read transaction.

Replaced the entire hosted/prototype/V2 Console facade tree with one typed API transport, disposable owner-scoped state/cache and server-derived projections. Setup is email/password + manual Modal + manual GitHub + Zen, with free catalog models preferred. Projects, Sessions, Conversation, Activity, immutable Changes/Delivery, Files, lease-local Terminal, Services/Preview, Child Sessions, Connections and Settings are connected to the unified services. No credentials are persisted in browser storage; fields clear after submission; only the theme is stored. Network retries reuse the mutation key/body, and event gaps/schema changes require a new snapshot.

File application, edits and supervised terminal/service actions have durable typed Worktree operation records and Jobs. PTY writers reserve the Worktree barrier; services use exact pinned declarations and minimal environment, logs are bounded/redacted. Preview proxy is a separate application/origin and does not forward Console cookies/admin headers. Checkpoint barriers now also prevent queued Turn admission during capture. Private immutable export and explicit ChangeSet application work independently of the author executor. SDK/CLI additions use ordinary resources and bounded Turn/result waits, no model loop.

Tests include API accepted-intent retry, CSRF, pure reads, reconstructed API process state, second-owner isolation, write-only validation errors, scoped keys/password revocation, PTY/barrier/stop behavior, Console setup/key clearing, reconnect dedupe/cache purge, uncertain-request idempotency. Console typecheck/test/build and generated spec checks run cloud-free.

Known limits: email transport is injected by the operator; public registration has no verification bypass. Interactive PTY UI currently uses bounded polling/input frames; the process is a real PTY, but a WebSocket attachment UX is not advertised. Preview is bounded HTTP, not a general WebSocket tunnel. Native cross-session MCP transport remains unadvertised; the same primitives are accessible through the product/SDK and a Session-scoped application gateway. Environment build caches and later extension/automation features are not advertised. Final phase retires the remaining legacy backend/SDK/CLI deployment roots and runs the real four-credential path.

Phase 5 results: full isolated suite **3543 passed, 14 skipped** (154.35s), with one pre-existing legacy watcher-thread warning (that machinery is deleted in phase 6); final unified focused suite **36 passed** after operation/spec changes. `make lint`, generated spec drift, Console typecheck, **4 Console tests**, and production build passed. All four Phase 4 GitHub CI jobs passed before progressing.
