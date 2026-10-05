# Architecture decisions, rejected alternatives, and risks

**Proposed, not accepted.** These records make tradeoffs reviewable without pretending implementation has been validated. Source classifications and pinned links are in [research.md](research.md).

## Decision log

| ID | Decision and rationale | Tradeoff / consequence |
| --- | --- | --- |
| ADR-01 | Session is real durable work identity; remove Task/Agent/Run/V1/V2 wrappers. Amp Thread is a strong product reference; current SBX already has Session UI. | Breaking API/client cutover and explicit data import. Preserve history where justified, not old mutation architecture. |
| ADR-02 | Session, Executor, and official CLI Harness are independent. Native thread ID is an opaque binding with versioned state. | Cannot promise seamless provider switching or context translation. Switching Harness means a linked Session/handoff. |
| ADR-03 | Python/FastAPI modular monolith, one database, one Job worker mechanism. | Shared process failure domain; modules/transactions and bounded workers must remain disciplined. Scale API/worker independently only as needed. |
| ADR-04 | Stable supervised `sbx-runtime` daemon with common process/files/services protocol. | Larger runtime surface than read-only SSE today; auth/fencing/version/upgrade gates essential. It supervises official CLI execution without taking over agent reasoning. |
| ADR-05 | Transactional Session journal with typed synchronous projections; relational config/auth/claims remain authoritative rows. | Reducer/schema evolution and event retention need policy. Avoid asynchronous projection correctness gaps and full-system event sourcing. |
| ADR-06 | One durable Job/lease/CAS protocol for effects and reconciliation. | At-least-once effects need explicit idempotency/evidence. DB fencing cannot stop unreachable CLI side effects; quarantine old leases. |
| ADR-07 | Versioned Project defaults and two Snapshot kinds: shared environment vs private Session checkpoint. | Exact input keys may reduce cache hits, but avoid undocumented source/setup or credential contamination. Optimize after measuring. |
| ADR-08 | Worktree is logical filesystem identity; immutable ChangeSet is review/Ship subject; Delivery owns integration effects. | Extra capture/storage compared with direct live git push; gains teardown-safe shipping and auditable independent gates. |
| ADR-09 | Review is generic child Delegation, with validated result and digest/head pin. | A separate CLI Session proves execution/context isolation, not different human identity or perfect write prevention. Policy must state required independence. |
| ADR-10 | Generic spawn/message/wait/result/cancel before declarative Workflow. | Coordinator needs provider MCP or command access; no central plan optimizer/DAG engine. Human/API-driven delegation works first. |
| ADR-11 | Manual Connections first, encrypted versions, owner/principal scope, optional OAuth later. | Manual token replacement is a user task; validation cannot universally prove all fine-grained token permissions. Never infer ready from configuration alone. |
| ADR-12 | Modal remains default Executor; local fake-backed lane implements same runtime boundary. | Modal filesystem/native-state restore has limits. Substrate is research, not an immediate backend commitment. |
| ADR-13 | Project-local services and authenticated preview, with browser testing as environment/tool capability. | Dedicated preview origin and WebSocket/log/PTY handling add engineering work. No mandatory Portal widget or separate Browser aggregate. |
| ADR-14 | One API/client/Console domain model and a finite migration. | Coordinated client release required; temporary cohort routing/read exports have deadlines. No indefinite compatibility facade. |
| ADR-15 | Provider-specific capability/version evidence, not universal Amp-style modes/plugins. | Some features unavailable on particular CLIs; UI must expose supported settings and explain constraints. Native tools/extensions remain provider-owned. |

## Rejected alternatives

| Alternative | Why rejected now | Evidence or condition that could reopen it |
| --- | --- | --- |
| Keep Task/Agent/Run and wrap them in a more elegant Session facade | Current V2 already demonstrates the extra aggregate/lease/cursor complexity. Contradicts clean-slate objective. | A compelling externally contracted compatibility requirement, explicitly authorized and time-bounded. |
| Build SBX's own model/agent loop or use Amp SDK as the execution core | Removes SBX's official CLI differentiator and assumes ownership of prompts/tools/provider billing. | A different product brief, not architecture cleanup. |
| Clone Amp core architecture from public repos | No inspected public repo contains its complete core. Public examples/extensions/infrastructure are not closed production internals. | Verified additional public core source with suitable license and clear scope. |
| Replace Modal with Substrate/Kubernetes immediately | Operational burden and aspirational upstream architecture are not justified by current SBX usage; no Amp deployment linkage proved. | Measured Modal cost/latency/restore constraints and independently validated backend value. Preserve port for that possibility. |
| Distributed microservices, Kafka, Redis/event bus, separate workflow service | More failure/ordering boundaries without current scale/use evidence. PostgreSQL transactions and bounded Jobs solve current ownership problems. | Measured bottleneck or isolation need that cannot be handled in the monolith. |
| Universal DAG execution engine before delegation | Forces rigid plans and duplicate task/run state before testing generic coordination needs. | Repeated real workflows needing versioned declarative recipes on top of existing primitives. |
| OAuth required for all Connections | Conflicts with minimum manual setup; couples product availability to integration registration/callback flows. | OAuth as optional acquisition UX when a connector requires/benefits from it. |
| Copy Amp's default direct-trunk Ship prompt | SBX has exact-head independent review/merge requirements. A prompt is not deterministic authorization or remote effect evidence. | Opt-in project policy with explicit target/gates; still uses ChangeSet/Delivery. |
| Keep Delivery inside Session/ChangeSet status | Conflates successful coding with failed push and complicates retry. | No current reason. A combined display is a projection, not shared lifecycle authority. |
| Special hosted review engine plus Review execution resource | Existing hosted review already uses Sessions but adds link/polling/Revision gates. | Distinct human-review product could justify a typed assessment UI, not another agent executor. |
| Only keep ephemeral direct runtime events for lowest latency | History/replay/cursor fails on compute loss and splits truth across transports. | Optional clearly provisional live hints after durable correctness is established; no alternate authority. |
| Snapshot all memory and assume secrets are gone after deleting auth files | Process/env memory may retain credentials; backend semantics differ. | A verified secure snapshot design with private encryption/isolation and actual backend support. |
| Shared writable repository for sibling agents | Edit races, ambiguous review subject, and native context leakage. | Explicit collaboration product with a conflict/concurrency model; not initial delegation. |
| Hosted Git service, voice/Puck personality, shared billing, plugin marketplace | No demonstrated current use; amplifies scope without fixing SBX ownership. | Separate product decision and adoption evidence. |

## Material risks and feasibility gates

| Risk | Consequence | Evidence required before claiming support |
| --- | --- | --- |
| Native official CLI resume is version/state-sensitive | Saved conversation might not resume after restore/upgrade. | Per-provider pinned two-turn restore tests, state manifest compatibility, exact native ID verification, stale-ID refusal. Keep unsupported/unknown states. |
| Proprietary distribution/credential portability | Some CLI images cannot be reproducibly built or require provider constraints. | Provider distribution rights/install evidence and isolated supported credential probes. No copying host binaries or declaring Claude registered from source presence. |
| Runtime exposed files/process/terminal surface | New mutation channels or path/proxy escapes. | Scoped grants/fences, root/symlink bounds, size limits, terminal input authorization, same-origin separation and preview target allowlisting. |
| Runtime executes code controlled by users/agents | Runtime evidence can be tampered with; permitted credentials may be read by sandbox code. | Keep platform/DB/master keys outside; scoped connections; control-plane external-effect gates. Do not market process results as cryptographic attestation. |
| Job expiry while CLI remains alive | Duplicate prompts/external side effects or concurrent worktrees. | Lost-claim/start-response/restart exercise; adopt by Execution ID or terminate/quarantine before replacement; test stale fence writes and runtime expiry. |
| Partial event ingestion before sandbox disappears | Missing output/history despite known accepted work. | Ack/watermark replay/dedupe/backpressure tests; explicitly incomplete terminal evidence and checkpoint recovery point; no false success. |
| Capture/snapshot competes with user shell or follow-up | Inconsistent files, secret scrub window, lost edits. | Filesystem barriers, generation checks, terminal mutation policy and queued-message tests; snapshot only after quiescence. |
| Shared rotating provider refresh credentials | Race can invalidate user auth or overwrite replacement. | Connector refresh lease/CAS ownership; replaced/revoked-version tests; static API keys never exported. |
| Setup cache contamination | Reuse another session's secrets/native context or wrong source. | Owner/input/digest isolation; secret-free environment cache; private checkpoint access and invalidation exercises. |
| Modal restore differs from Orb product promises | Preview/terminal or CLI state fails to return; unpredictable cost/latency. | Live owner-bound Modal filesystem restore + restart services + native context gate. Measure cold/warm latency/retention/storage cost; promise no full memory/process restore. |
| Patch-only ChangeSet delivered as new commit | Review pin does not match remote subject. | Digest/tree-to-commit mapping and remote expected-head gate; rebases/conflict fixes become new ChangeSets. |
| GitHub PAT rights change or effects are ambiguous | Failed push/PR/merge, duplicated PR or unauthorized effect. | Scope-aware errors, deterministic remote discovery/head checks and merge preconditions; no blanket capability guarantee from read probes. |
| Legacy data lacks events/native checkpoints or has conflicting mirrors | Import cannot recreate exact history or trustworthy eligibility. | Rehearsed import report with provenance/incomplete flags, artifact digest checks and remote reconciliation; deny guessed approval/success. |
| Journal/projections diverge during schema evolution | Incorrect UI/admission or invalid rebuild. | Transactional append/projection tests, versioned reducer replay and precondition/unique-claim checks; don't mix future event schemas silently. |
| Snapshot/ChangeSet/object retention costs | Durable work persists beyond compute lifetime. | Explicit quotas/retention/reference accounting and cleanup evidence; immutable content doesn't mean retain every large raw trace forever. |

These are risks of the proposed implementation, not findings that current production has every listed defect. No cloud credentials, live provider turns, external Git changes, or Modal provisioning were used to validate this research.

## Non-goals for the first unified release

- A custom model/agent harness, model gateway, or translated cross-provider hidden context.
- Kubernetes/Substrate replacement, microservices, event-streaming cluster, or generalized infrastructure scheduler.
- OAuth-required onboarding, team billing, Amp-hosted repositories or arbitrary third-party integration marketplace.
- A complex DAG engine, migration knowledge graph, autonomous organization-wide Puck, or native subagent mirroring without provider evidence.
- Perfect cross-backend memory/process snapshots or zero-cost indefinite retention promises.
- Full browser desktop architecture, Portal DOM control injection, final visuals, voice, or multiplayer sessions.
- Every provider enabled immediately; unsupported distribution/auth/resume remains honest and capability-gated.
- Permanent v1/v2 compatibility or indefinite dual write/recovery architectures.

## Architecture review focus

Review whether Session/Executor/Harness separation is consistently maintained, whether each mutation/effect has one authority, whether Project cache and private checkpoint reuse are isolated, whether review/Ship pins remain sound for patch and Git subjects, and whether the cutover actually deletes legacy layers. Concrete provider/runtime/Modal feasibility gates can narrow capabilities without reopening the domain model. This PR is a discussion artifact; approval of the prose does not authorize rollout or merge.
