# Decisions, resolved disagreements and feasibility risks

**NORMATIVE decisions; INFORMATIVE analysis and risk estimates.** [Canonical index](README.md). These are settled target decisions for implementation after human review, not measured production capability claims.

## Consolidated decision ledger

| ADR | #165 direction | #166 direction | Final decision and reason |
| --- | --- | --- | --- |
| U01 identity | rename durable work Thread | durable SBX Session | **Session MUST be core**. Amp Thread is source mapping; no Task/Agent/Run facade. Preserves product naming without legacy ownership. |
| U02 compute | Machine 1:1 with Thread, pause/wake product | replaceable lease plus logical Worktree | **ExecutorLease + Worktree MUST be independent of Session**. No durable machine ID; lost lease does not lose conversation. |
| U03 provider boundary | official CLI Driver families | official CLI Harness protocol | **Harness MUST name the single native adapter boundary**; shared JSONL/ACP/official-server transport helpers allowed. Runtime is infrastructure, never reasoning. |
| U04 runtime | `sbxd` broad daemon/runner protocol | supervised `sbx-runtime` with validated evidence | Adopt stable daemon responsibilities and cross-backend protocol, named **sbx-runtime**. No ad-hoc exec pattern or business state in daemon. |
| U05 modes | first-Turn-frozen Mode = driver/settings/tool set | explicit settings/capabilities; presets convenience | **ExecutionPreset MAY package defaults; MUST NOT be durable core mode**. Harness lineage fixed, compatible model/effort changes recorded per Turn. |
| U06 ownership/recovery | Thread actor as serial advancement owner | DB-authoritative Jobs/claim/fence/UoW | **DB state + Jobs MUST be authority**. Actor allowed only as optimization; no actor-memory/startup/reaper/review/delivery parallel engines. |
| U07 journal | normalized thread events, Codex/Amp-shaped item vocabulary, raw retention | provider-neutral journal plus runtime evidence and typed projections | Adopt one committed Session journal and synchronous typed projections. Stable message parts/tool observations; raw private traces optional, bounded/redacted. No post-upgrade rewriting committed history. |
| U08 shipping | Ship/Review/Restack prompts and Git observation | immutable ChangeSet, platform Delivery, remote verification | **Delivery MUST own authorized effects**. Prompts may prepare/conflict-resolve only. Exact-subject and stale-head gates survive. |
| U09 reviews | first-class Review entity referencing review thread | validated typed DelegationResult | **No Review execution subsystem**. Assessment stores subject/verdict/checks; child Session ordinary. Review can precede PR. |
| U10 coordination | rich built-in MCP spawn/send/wait/read/file tools | generic durable Delegation with budgets/waits | Absorb strong tool/product ideas under **ordinary Sessions/Jobs/grants**. Cross-CLI Coordinator role, not private actor/DAG. |
| U11 connections | provider Connection plus separate GitHub/Modal Integration; subscription login emphasis | one Connection + encrypted versions; manual-first | **One Connection MUST cover all external authority**. Manual Modal/GitHub/Zen, optional Codex; OAuth/device acquisition later. No Integration aggregate/fallback. |
| U12 refresh | optional expiry-based merging of concurrent exports | supported exclusive refresh authority/version CAS | **CAS + revocation epoch MUST protect writeback**; rotating tokens require verified semantics/refresh claim. No newer-expiry/last-writer-wins assumption. |
| U13 snapshots | Orb-like pause/services restore language | conservative file/native-state checkpoint and restarted services | Adopt separate cache/checkpoint reuse with digest/owner verification. **No memory/PTY/process restore promise** or secret-bearing memory capture. |
| U14 API | fresh `/v1/threads`, stream compatibility projection | one `/api` Session surface | **One `/api` business API MUST be target**. Runtime wire version independent; optional lossy client stream projection not canonical ontology. |
| U15 repository | relocate into packages/core/server/drivers workspace | retain pragmatic top-level names, rewrite internals | **Keep control/runtime/console/src/sbx** and full clean internal boundaries. Naming continuity doesn't preserve layering. Add explicit extension/tooling boundaries. |
| U16 extensions | plugin/mode/automation first-class early packages | defer optional layers until reliable rewrite | Native instructions/tools/Delegation core; **packaging/marketplace/triggers later** on same primitives. No plugin engine prerequisite. |
| U17 cutover | parallel new packages, compatibility translator | one writer/cohort split/finite read export, delete old | staged rewrite then data cutover then **DELETE**; experimental runner bridge gone R3, all compatibility gone R7. No dual writes/V2-over-V1. |
| U18 public research | Amp public repos/types product reference, some broad equivalences | pinned facts/docs/inference and license discipline | Preserve #166 exact pins, freshly verify #165 type/skill evidence, distinguish public API from private core and aspirational infrastructure. **No upstream copying**. |

Disagreement resolution changes the architecture rather than concatenating it: there is no Machine alongside ExecutorLease, Mode alongside explicit settings, Review engine alongside Delegation, Integration alongside Connection, ThreadActor alongside Job authority, or prompt Ship alongside platform effects.

## Rejected alternatives and reopening criteria

| Alternative | Decision | Evidence that could justify a later amendment |
| --- | --- | --- |
| Proprietary SBX model/agent harness or Amp SDK execution core | MUST NOT implement; contradicts differentiator | requires a changed product brief, not an adapter feature |
| Preserve Task/Agent/Run plus unified facade | MUST delete at cutover; repeats actual V2 layering | explicitly authorized finite external compatibility obligation, never permanent domain |
| Native thread/PR/machine as Session ID | MUST NOT use | no anticipated requirement justifies coupling independent lifetimes |
| Amp Substrate/Kubernetes now | out of scope | measured Modal limitation + validated alternative value and new backend review |
| Microservices/Kafka/Redis scheduler now | out of scope | measured bottleneck/isolation need not solved by API/worker scaling and PG |
| Independent Workflow DAG or hosted review scheduler | MUST NOT introduce | recipe/compiler may be added atop primitives after repeated use; no second authority |
| Direct runtime events as another public truth | MUST NOT retain | optional clearly provisional hints only after durable correctness, never outcomes/cursors |
| Shared mutable sibling Worktree | MUST NOT use initially | explicit collaborative concurrency/conflict model and product requirement |
| Universal pre-tool policy interception | unsupported until provider evidence | official CLI/version protocol + prevention/cancel gates |
| Required OAuth/App broker | not MVP prerequisite | optional connector acquisition with demonstrated value; same Connection |
| Full process-memory hibernation | not baseline | backend/security evidence proving secret isolation and accurate restoration contract |
| Copy Amp code/prompts/type implementations | not needed/authorized | separately reviewed license grant and reuse proposal; public availability insufficient |

## Feasibility risks and required evidence

| Risk | Consequence / conservative stance | Required acceptance evidence |
| --- | --- | --- |
| CLI state/version/account sensitivity | loss of native continuation; linked continuation is explicit fallback | per-provider two-Turn same-ID resume, checkpoint/restore, mismatch refusal and version compatibility matrix |
| Proprietary install/license/access limits | source module does not prove distributable/supportable CLI | reproducible exact-version build/install evidence, supported auth probes, no host credential/binary assumptions |
| Offline old runtime continues effects | DB fences alone cannot stop process tools | quarantine/reconcile/termination tests, no slot reclaim/replacement before isolation evidence |
| Crash between spawn and start journal | duplicate execution if acceptance misread | process/native reconciliation for accepted-but-ambiguous operation; no relaunch |
| Spool loss before ingestion | conversation/output/files may be incomplete | final watermark/ack/dedupe/backpressure tests; interrupted outcome and visible recovery point |
| Snapshot/capture with user/service writers | inconsistent or secret-contaminated payload | exclusive barriers, writer revocation/quiescence, fail capture when unverifiable, known-secret guard gates |
| Arbitrary code in sandbox | permitted credentials may be read, runtime evidence may be forged | no platform/admin/master secrets in sandbox, attenuated grants, platform-owned merge policy; no attestation marketing |
| Rotating refresh sharing | invalid token chain or overwritten replacement | refresh ownership lock/CAS/revoked-version tests; restrict slots when unverified |
| Cache reuse or restore drift | another Session's context or wrong source | exact owner/input digests, no personal context in cache, checkpoint priority and access checks |
| Git push/PR/merge ambiguous response | duplicate PR or false delivered/merged state | deterministic association/head preconditions/discovery; unresolved status rather than speculative repeat |
| Patch materialization / rebase | approval doesn't apply to remote subject | canonical digest vectors, exact tree/baseline→commit mapping, new subject after rewriting |
| Remote check/base race | stale eligibility between observation and effect | exact-head remote enforcement, provider-specific base policy; block unsupported atomic requirement |
| Legacy mirrored/conflicting data | guessed history/resume/approval | rehearsed import/provenance/loss report, secret/hash/owner checks, conservative imported eligibility |
| Relational/journal evolution | current state diverges from replay | atomic rollback/versioned reducer/rebuild tests, typed constraints, reset semantics |
| Preview/files/PTY surface | origin/path/grant escape | symlink/archive bounds, owner/lease expiry tests, dedicated origin, allowlisted ports/headers |
| Blob/native-state cost and privacy | long-lived content beyond compute | quotas/reference retention/encrypted private native state; explicit deletion boundaries |
| Modal performance/restore gap | cold/warm latency or services fail | opt-in live owner-bound file/native restore and restarted services, measured cost/latency; no Orb-equivalence claim |

None of these rows claims a newly found production bug. They are implementation gates for new capabilities. Failure of a capability gate MUST narrow support honestly; it MUST NOT reopen Session/Executor/Harness identity or introduce a compatibility subsystem to hide the limitation.

The remaining choices are concrete libraries (UoW/ORM/transport implementation), measured frame sizes/batch delays/timeouts, retention/cost defaults, provider rollout order and optional UI polish. Implementers MUST specify/test those within this architecture. They do not need to re-decide core identity, mutation ownership, journaling, jobs, credential boundaries or exact-subject semantics. Human review should assess those architectural decisions before approving implementation scope.
