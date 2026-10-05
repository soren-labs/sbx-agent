# Migration architecture and finite cutover

This describes architectural sequencing and acceptance evidence, not implementation tickets or code-level steps. The endpoint is one architecture. Temporary bridges must have an owner, removal gate, and expiry/release boundary. No permanent Task/Agent/Run facade or v1/v2 compatibility promise is part of the target.

The current frozen contracts remain unchanged by this proposal. A later implementation must get explicit technical review for replacement contracts and schema/data migration. #164 remains independent: its manual-credential product requirements can be represented in the new model whether its implementation merges before or after unification.

## 1. Establish replacement contracts and data interpretation first

Agree on Session, Project, Turn/Execution/lease, Worktree/Snapshot, ChangeSet/Delivery, and Delegation definitions in this proposal. Establish one public API schema, a runtime protocol handshake, provider Harness capability evidence, canonical event envelope, and ownership/credential rules. Preserve existing provider version pins, secret guards, exact-head review semantics, output-contract checks, and outcome-unknown behavior as requirements.

Choose an ownership migration rule: existing hosted user → personal Workspace; operator-owned legacy records require explicit assignment/export, never guessed from a repository URL. Map legacy stable public Session IDs where available. No new dual identity translation is needed for future Sessions. Archived legacy records can retain an `imported_reference` value for traceability without keeping old handlers.

Before any cutover, define supported import scope and data loss limits. Current persisted transcripts may not contain every original event; current JSONL line cursors cannot become an identical new Session event history. Imported events need provenance and an explicit import marker. Missing native state/checkpoints means read-only historical work or explicit linked continuation, not a manufactured successful resume.

**Exit evidence:** reviewed vocabulary/protocols, migration mapping for real persisted record shapes, deterministic ownership and conflict rules, and a clear support scope for each official CLI. This phase does not require moving every module.

## 2. Introduce durable transactional seams

Make typed Session/Turn/Event persistence, command dedupe, Jobs/claims, Executor leases and capacity reservations the replacement mutation foundation. Reuse the current auth/vault concepts behind explicit Connection interfaces. Introduce the runtime client/Executor port without changing provider reasoning.

A short-lived bridge may translate the new dispatch command to existing runner invocation to validate the durable boundary. It must be one directional: new application authority owns the accepted intent; the old runner is an effect adapter. Do not let new and old APIs mutate the same Session independently. Do not build a new Session projection over old Task state and declare that the endpoint.

**Exit evidence:** accepted intent survives API/worker restart; operation ambiguity is reconciled before rerun; cancellation/lease fences hold; owner-bound credential materialization has no fallback; no read path creates effects. A prototype bridge has a retirement gate in phase 3.

## 3. Cut over runtime and official Harness boundary

Implement the daemon protocol/common supervisor and relocate thin official CLI adapters behind it. Keep native CLI transport differences (including ACP) and capability reporting. Introduce credential-safe checkpoint barriers and normalized spool ingestion. Exercise local executor + fake official CLIs, then opt-in live Modal/provider gates with independent credential isolation.

Cut over one complete execution lane for **new Sessions**: accepted Message → durable Job → current lease → runtime → official CLI → ingested result → checkpoint. The new lane must never call back into old route functions, V1State, or Task status reducers. Existing active legacy work drains on old images and old mutation authority, with a bounded deadline; new work only enters the target lane. No live Session is simultaneously dispatched by both architectures.

**Exit evidence:** two native turns, context identity verification, cancellation, reconnect/event replay, lost start response, restored native state, queued follow-up during checkpoint, runtime compatibility rejection, stale worker rejection, and honest outcome after sandbox loss. Remove the runner-invocation bridge and old runtime event transport for target Sessions. Support tier differences remain explicit; do not enable every provider merely to match a list.

## 4. Unify environments, changes, shipping, and child work

Represent reusable settings as Project versions and environment Snapshot caches. Represent session recovery as private checkpoints with the same manifest/storage machinery and different reuse policy. Move Git capture into ChangeSet and remote effects into Delivery. Add generic Delegation and result validation; implement review as a preset with pinned immutable input. All background behavior uses Jobs.

Import existing Revision/artifact payloads into ChangeSets after integrity/owner/secret checks. Workspace publish mirrors become historical Delivery evidence; prefer verified authoritative Revision delivery data where consistent. Conflicting remote records remain diagnosed and cannot authorize merge. Review import pins the actual immutable subject; a SHA-only review of an uncommitted patch must not be promoted to content approval without matching evidence.

**Exit evidence:** ChangeSet readable and Delivery retryable after author compute teardown; cancelled-work automatic Ship denied; remote head drift blocks merge; child results survive restart; review before PR works; Project cache never contains private credentials/native context; Session restore never resets work to current Project main. Delete Revision/Workspace delivery mirrors and hosted-only review orchestration for target data.

## 5. Replace API, Console, clients and stored data once

Switch Console and supported client releases to the unified `/api` resources together. Projects/Sessions/Children/Changes/Services/Connections map to real entities. Replace history normalization with one committed event watermark and server eligibility. Retain useful UI behavior/accessibility, not server-version facades.

Use an explicit cutover window for remaining legacy data: pause old mutations, drain/stop active old work, export final records/payloads and scrubbed checkpoints, import transactionally with reconciliation report, and validate counts/ownership/status/digests. Legacy API clients receive an intentional retirement response or a bounded read-only export window, not permanent write compatibility. A documented upgrade/export path is a product reason to preserve history, not preserve old APIs.

**Exit evidence:** only target authorities can mutate a resource; imports have provenance; read-only history remains accessible where promised; no secrets/plaintext auth appear in events/blobs; no status guessed successful. Reconcile remote PR mappings and unresolved outcomes before permitting shipping. Native context import is optional per verified compatibility; history continuity and native resume are different claims.

## 6. Delete old architecture and close the migration

Delete `api_v1`, `api_v2`, V1State, Task/Agent/Run business wrappers, namespace stores/index repair, Basic dashboard/web frontend, old hosted wrapper routes, startup/lifecycle/delivery/review loops, old shell runner orchestration, and Console split facades. Consolidate deployment/client/docs references and retire old contracts explicitly into historical documentation. Keep provider recordings, release/support evidence, relevant recovery tests, and necessary auth/encryption implementation concepts.

Deletion gate is architectural as well as functional: no imports of old handlers/private locks, no old mutable namespace records, no dual cursor scheme, no timer directly dispatching effects outside Jobs, and no implicit hosted Codex requirement. The new source tree must have a single owner for each current behavior. Operators need one queue/claim/lease diagnostic view, not separate review/startup/reaper dashboards.

**Exit evidence:** cloud-free lint/test and domain/transaction/runtime/Console gates green; live provider/Modal gates for supported lanes; persisted state restart/cancel/review/Ship exercise; docs and clients describe one vocabulary; temporary bridges removed. This phase completes unification rather than leaving a "new architecture" alongside the old.

## Cutover and rollback rules

| Stage | Safe rollback | Explicit limit |
| --- | --- | --- |
| Before target Sessions receive traffic | Discard/rebuild experimental target state and revert rollout. | Existing production remains the only authority. |
| New-target Sessions alongside draining legacy cohort | Route each existing Session to its sole owner; stop new target creation if needed; fix/forward-recover target Sessions. | Routing is a time-bounded cohort split, not two permanent products. Do not route target data to old reducers. |
| Final import, before target writes reopen | Restore verified pre-cutover DB/blob backups and old image configuration. | No remote effects during this validation window; snapshot references need compatible owners/images. |
| After target writes or Git effects | Prefer forward repair. Downgrade requires stop/drain/export and an explicitly validated reverse data/effect plan. | Reverting application code alone cannot undo schema changes, a native CLI upgrade, Git pushes or merges. |

Schema expansion/data import may occur before deletion, but each resource has one writer. Avoid uncontrolled dual-writing events into both state models. Rehearse imports on credential-stripped data copies; external credential ciphertext remains protected and rotation/decrypt policy explicit. No production migration, rollout, schema or config is performed by this PR.

## How to avoid re-deciding the model later

Treat the three-way Session/Executor/Harness separation, authority table, lifecycle definitions, subject pinning, Project-versus-checkpoint distinction, and single Job mechanism as the stable architectural decisions. Later efforts may select concrete transport/storage libraries or tune cache/timeouts without inventing another Task wrapper. If a feasibility gate disproves a capability (such as native resume portability), record that limitation in the capability model and recovery UI; do not hide it in a parallel architecture.
