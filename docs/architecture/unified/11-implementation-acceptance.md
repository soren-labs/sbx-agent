# Implementation acceptance and RFC validation

**NORMATIVE gates; final documentation-validation record is INFORMATIVE.** [Architecture index](README.md), [cutover gates](10-rewrite-cutover.md).

Acceptance MUST verify behavior under faults, concurrency and restart, not only happy-path schema conformance. A passed prose/diagram check does not certify runtime implementation. Future changes MUST run cloud-free lint/test and relevant contract/integration/Console checks; opt-in live gates require separately scoped credentials and exact image/CLI/version evidence.

## Required scenarios and pass criteria

| Gate | Scenario / fault injection | Required result |
| --- | --- | --- |
| A01 — accepted intent | crash API after commit before response; retry same key; retry changed body | same Message/Turn/Job IDs; one dispatch; changed body conflict; queued input survives |
| A02 — concurrent queue/admission | parallel follow-ups, workers, last provider slot and quota claims | stable ordinals; one active Turn/Execution per Worktree; no slot/quota oversell; unadmitted requests visible as queued |
| A03 — transactional authority | fail transaction between projection/event/Job/outbox writes; rebuild reducers | all-or-nothing commit; no lost accepted work/phantom success; rebuilt current state matches typed projections |
| A04 — claim loss/fencing | worker loses claim during external call; successor claims; stale worker completes | stale commit rejected; same effect inspected/adopted; no new competing Execution |
| A05 — allocation ambiguity | allocate succeeds, response/bind lost, worker restarts | operation-tag lookup adopts exact lease or exact orphan cleanup; no blind second allocation |
| A06 — native start ambiguity | runtime fsync acceptance then crash around spawn; start response dropped | query/reconcile same Execution/process/native binding; ambiguous case unknown/quarantined, never auto-replayed |
| A07 — cancel/success race | run both transaction orders; late success/spool replay; unable to stop | committed success wins only if first; earlier cancel prevents business success/auto-Ship; confirmed stop cancelled, unconfirmed interrupted |
| A08 — event replay | duplicate/out-of-order source batches, dropped ack, control restart, reducer reconnect | dedupe source tuple; ack contiguous committed evidence only; one Session seq; stable part revisions and no duplicate text |
| A09 — terminal watermark | omit a frame through final watermark, lose spool/runtime | no normal complete success until required evidence persisted; incompleteness explicit; no automatic Delivery |
| A10 — pure reads | repeat every list/detail/events/review-result/file/preview GET with missing/stale compute | no allocation/settle/validation/result publication/delivery/domain writes; timestamp/unavailable diagnosis |
| A11 — native CLI continuation | first Turn stores unique marker, second refers to it; checkpoint, destroy runtime, restore third; wrong native ID/version/account | correct native lineage and file context verified; mismatch/unsupported resume refused; explicit linked continuation only |
| A12 — provider honesty | unsupported/unknown steer/MCP/approval/usage/model/effort/native-state export | capabilities/UI/API accurate per installed version/Connection; explicit unsupported/fallback; no fabricated usage/reasoning/tools |
| A13 — runtime protocol rollout | incompatible major/image, supported minor, stale lease grant, duplicate changed operation body | incompatible refuses before mutation; negotiated minor; stale mutation denied; ID/body conflict |
| A14 — runtime loss | kill daemon/executor mid-turn; old executor unreachable; new dispatch | conversation/history retained; interrupted outcome/recovery point; old compute quarantined and slot reserved until isolation; no concurrent writer |
| A15 — spool pressure | fill disk/buffer, disconnect control, continue output | bounded diagnosed stop/intake backpressure; no silently dropped unacknowledged terminal evidence |
| A16 — checkpoint barrier | queued follow-up while scrub/upload; PTY writer/service/files race; failed upload | input persists; one barrier; no dispatch during scrub; unverifiable capture fails; prior checkpoint stays authoritative |
| A17 — environment/cache | modify source/setup/dependency/image; restore after Project main advances; sibling restore attempt | exact-key invalidation; Session checkpoint takes precedence; no work reset; no private/native context cache sharing |
| A18 — credential isolation | two owners/all providers, Modal and GitHub versions; ambient env present in parent; exception/trace/snapshot paths | runtime gets only authorized selected material; no platform/Modal/master keys; HOME/XDG isolated; no secret output or snapshot/ChangeSet |
| A19 — replacement/revoke | old refresh/validation completes after replace/disconnect; pending grants/Jobs; static key export | version/epoch CAS denies stale completion/writeback; no old OAuth/App/host fallback; static keys no writeback; cleanup status honest |
| A20 — capture integrity | binary/untracked/deleted/renamed/symlink/credential file; tampered/missing blob | exact canonical digest/files; exclusions/escape/integrity enforced; no fake ready subject; Turn verdict independent |
| A21 — exact subject | review patch-only capture then materialize commit; amend/rebase/extra file; malformed ResultContract | exact mapping passes; changed subject needs new ChangeSet/review; invalid result never approval |
| A22 — Delivery retry | author lease terminated; push/PR response lost; worker dies between steps | immutable payload usable; verify existing remote effect; one PR association; retry transport without coding Turn |
| A23 — target concurrency/stale head | concurrent Deliveries/merge, remote actor changes head/base/check/draft after eligibility | target claims serialize platform writes; exact remote preconditions deny stale effect; UI badge cannot authorize |
| A24 — cancelled/salvage policy | cancelled/failed/interrupted Turn has changed files; explicit salvage | auto-delivery denied; explicit salvage captured with ineligible origin; new authorized policy gate required |
| A25 — generic child work | review/test before PR, different supported Harness, isolated copy, child writes fixes | pinned input/result contract; no parent mutation; ordinary Session machinery; child fixes explicit transfer/new subject |
| A26 — waits/results restart | register wait concurrent with child completion; repeated tool call; parent/worker restart; timeout/subtree cancel | no missed/double wake; one validated final result; budgets/depth/grants enforced; no worker sleep/phantom capacity release |
| A27 — files/PTY/preview | path/symlink/archive escape, cross-owner ID, expired lease grant, arbitrary host/port, malicious preview app | denied escapes/authority; separate origin; no Console cookie/admin header leakage; replacement PTY explicit |
| A28 — one Console state | snapshot plus event race/gap, cumulative frame reconnect, uncertain Message POST, logout/access loss | watermark-consistent replay, no double append/new Turn, cache purge; server success/gates only |
| A29 — import | inconsistent namespace mirrors, missing transcript/native state/ownership, rerun importer | deterministic provenance/conflict/loss report; idempotent mapping; no guessed success/resume/approval; unresolved effects blocked |
| A30 — deletion/dependencies | scan deployed imports/routes/stores/timers/client names and active spec index | no legacy wrappers/dual cursors/read effects/bridges; target module rules enforced; R7 complete |
| A31 — later triggers/extensions | repeated schedule/webhook, handler crash after command, archived target, malicious package hook | durable receipt/occurrence dedupe; ordinary Message/Turn; shared claims; grants/timeout/isolation; no second engine |

Each gate MUST record exact code/image/runtime/Harness versions, scenario, result, failure limits and relevant evidence IDs. Fault tests MUST use fake official CLIs with deterministic scenario control; golden provider fixtures MUST contain `REDACTED` values. PG tests MUST use actual concurrent transactions/process restart where required, not a mocked single-thread mapping.

## Supported-provider release matrix

Every enabled official CLI MUST publish support tier (`supported|experimental|disabled`), exact distribution/install digest/version, credential methods, transport, two-turn native resume evidence, cancellation/timeout behavior, output/error classification, tool/skills/discovery/usage capability evidence and snapshot compatibility. A provider without verified native continuation MUST be labelled limited and MUST refuse unsupported follow-up semantics. Declaring support is a release gate, not a consequence of source file existence.

Cloud-free suite MUST cover all adapters' recorded normalization, credential invalid/rate-limit distinction, unknown valid frame handling, malformed streams, dropped terminal evidence and cancellation. Live Modal tests MUST separately verify each advertised supported lane's installation/auth, selected-owner credentials, first/second Turn native IDs, file/native-state checkpoint restore, declared service restart, stop/cleanup and no leakage. Claude/Grok/Antigravity/Devin distribution/capability uncertainty must remain visible. Live tests are opt-in and MUST NOT be imported/connected during ordinary test runs.

Manual MVP acceptance MUST demonstrate verified email/password → manual Modal + GitHub + Zen connections → OpenCode Session → queued follow-up → capture → independent child review/test → draft Delivery → exact-subject merge request under policy. Optional Codex may replace the Harness if enabled; removing Codex connection MUST NOT break non-Codex onboarding. Public/projectless and cloud-free Local flows must also work without unrelated Connections.

## Operational and migration release evidence

Operators MUST be able to inspect one queue/claim/fence/lease/effect diagnostic view with safe IDs/reasons. Recovery drills MUST cover DB backup restore plus private blob integrity, expired claims, quarantined compute, incompatible images, credential key rotation/revocation and unresolved Git effects. Cold/warm activation latency, spool lag and storage/compute cost SHOULD be measured before promising performance. No assumed Amp Orb performance target is inherited.

Rewrite completion MUST include coordinated API/SDK/CLI/Console release, importer rehearsal, owner/data loss report, remote ref reconciliation, old traffic/compute drain, bridge expiry and deletion scan. Acceptance of the architecture alone MUST NOT authorize production rollout. A human architecture review and subsequent implementation technical/release review remain distinct.

## This RFC's validation record — INFORMATIVE

The checks here validate documentation scope/provenance/consistency, not a running target. No provider Turns, Modal allocations, production migration, Linear changes or merge were performed. The user explicitly requested docs-only validation and avoiding unchanged huge suites; current runtime lint/test suites were not rerun. #166's historical test result is not presented as this branch's test execution.

Local validation completed on 2026-10-05. The external delivery receipt also contains the PR URL/HEAD and file list. These results apply to the documentation diff only.

| Documentation check | Method / result |
| --- | --- |
| Proposal/main heads | Git fetch + `rev-parse` and GitHub PR `headRefOid`: all three requested SHAs matched; recheck at publication |
| Proposal completeness | 10 files per exact head read; 20 source proposal files, no cherry-pick |
| Public source pins | 9 commits + 33 #166 file URLs verified, plus 3 #165 extra skill files; pinned root trees/license paths inspected |
| Additional types | exact @ampcode/sdk and @ampcode/plugin versions/type SHA-256 recorded in [evidence](01-evidence-and-comparison.md) |
| Documentation URLs | PASS: 22 successful public-doc retrievals, including all 17 #166 URLs; two guessed paths corrected explicitly; retrieval hashes recorded |
| Diff scope | only 13 Markdown files under `docs/architecture/unified/**`; no source/config/frozen-contract changes |
| Whitespace | PASS: `git diff --check` and `git diff --cached --check` |
| Internal links | PASS: all 51 relative Markdown links resolve; no unresolved fragment references |
| Mermaid | PASS: all 13 diagrams parse with Mermaid 11.15.0 in external isolated Node/jsdom environment; rendering not claimed |
| Secret/private-key signatures | PASS: private-key/GitHub/provider-key/AWS-key/JWT/credential-assignment patterns absent; source commit/type hashes are provenance |
| Upstream source reuse | PASS: diff has only original Markdown; no vendor/source/assets; no matching eight-line non-diagram code blocks against inspected upstream source/types, plus original-design review |
| Runtime regression suites | not run: unchanged code, explicitly docs-only task |

Validation tooling/downloads MUST stay outside the repository. Publication MUST remain one **draft** PR against main, marked not for merge until human architecture review. The source proposal PRs remain open research artifacts; #164 and Linear are untouched.
