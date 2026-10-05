# GPT benchmark implementation acceptance

The six stacked phases implement a running four-credential MVP against RFC #167,
frozen at `bf8cbe065da2f4c6bbca3781b4dce90cc31d39d3`. All thirteen canonical
documents remain unchanged. No competing track was used and no benchmark PR
was merged. This is implementation evidence, not production rollout approval.

## Stack and version evidence

1. [Phase 1 — Foundation](https://github.com/soren-labs/sbx-browser/pull/168)
2. [Phase 2 — Runtime](https://github.com/soren-labs/sbx-browser/pull/170)
3. [Phase 3 — Connections](https://github.com/soren-labs/sbx-browser/pull/171)
4. [Phase 4 — Delivery/Delegation](https://github.com/soren-labs/sbx-browser/pull/174)
5. [Phase 5 — Product](https://github.com/soren-labs/sbx-browser/pull/176)
6. Phase 6 — `benchmark/gpt-rfc167-06-mvp`, based on Phase 5; its final PR comment
   and local session artifact record the exact final HEAD and CI receipts.

The preserved complete live run used clean code commit
`0d27052cec73e0f0a3ae43724f18c0e158e6c611`, protocol 1.0, official OpenCode
1.18.29, adapter 1, and the actually usable free Zen model `opencode/big-pickle`.
Its runtime source digest was
`sha256:2180f17a13c981fd59b4eae8a5984b00c690017fe031ea15db7e5709c62c4c17`.
That run's `image_digest` field also held the source digest; it is not OCI image
provenance. The subsequent clean `12debb3538547204400877994c6dbe7b7a0bb539`
run recorded actual built Modal image `im-HfwYn2xU9PFuuFq2TUQKQt` separately
from runtime source digest
`sha256:2ec422d353e68deab4de4490e5362dd90591b089b755f0e7d4209511b879cfb1`.
It verified coding, native follow-up, sandbox tests and delivery before its
child runtime became unavailable; it is recorded as a failed repeat, not another
complete pass. Final changes add exact finished-allocation recovery, diagnostics
and reporting; they do not change the passed runtime's coding semantics.

OpenCode distribution integrity is pinned in `runtime/harnesses/opencode.py`;
installed version is verified before launch. Other Harnesses are disabled.
Modal SDK is pinned to 1.5.5: its public list omits finished sandboxes, so a
narrow exact-tag `SandboxList(include_finished=True)` RPC is required for lost
handle recovery. The recovery test and actual cleanup exercise this boundary.
Local checks used Python 3.12.3, disposable PostgreSQL 17 and isolated HOME/XDG.
Real browser checks used Chromium 145 through isolated Playwright 1.58.2.

## Minimum real MVP: all twenty requested criteria passed

[Preserved safe live evidence](gpt-real-mvp-evidence.json) records the full path:

| User criteria | Evidence |
| --- | --- |
| 1–5: email/password, three manual Connections, no Codex | Product HTTP login/store/validate; only Zen, Modal and GitHub Connection kinds exist |
| 6–9: usable free model, Project/Session, user Modal, official CLI | Free `opencode/big-pickle` actually inferred; selected stored credentials; real Modal daemon and official OpenCode coding Turn |
| 10–12: follow-up, tests, immutable capture | Native ID `ses_ef3aa70b6ffeCOQMPPk4kxJhCR` persisted; observed sandbox unittest exit zero; ChangeSet `cs_bdc4c9b11a364349b090a13c547f885c` |
| 13: branch/push/draft PR | [Disposable PR #27](https://github.com/soren-labs/sbx-e2e-test/pull/27), exact head `a0955926cd05228844c610d734392e5d8439cb88`; Git Database API materialization/ref push |
| 14: independent exact-subject child | Delegation `del_c515bd011f73481f9910dc69d8ee9260`, validated result `res_125b59014d0a423d9c8be6113cc8c938`, verdict `approve`, distinct execution/native/lease/Worktree |
| 15: subject/merge/reconcile | Stale subject rejected; reconcile and exact merge intent exercised; draft merge blocked with `remote_not_mergeable`; no merge was performed |
| 16–18: restart, second owner, disconnect | Process reconstruction retained manual Connections/Session; second owner denied nine resources; live Modal disconnect rejected while teardown authority remained needed |
| 19: no secret leaks | Response/event/log scans and browser DOM/input/storage checks; five known secret values absent from the entire stacked diff and retained raw logs |
| 20: cleanup | Author/child compute teardown confirmed, disposable PR closed and branch deleted; screenshots/encrypted local evidence retained privately |

The browser actually logged in, selected the three stored Connections and free
model in the composer, checked a 390px layout, and logged out. Its screenshot
remains private; no credentials or browser trace are published. The supplied
benchmark user was provisioned as verified by the trusted operator in a fresh
disposable database; public registration has no verification bypass.

The incomplete repeat is preserved separately in
[cleanup evidence](gpt-repeat-cleanup-evidence.json). After session interruption,
the existing cleanup Job exhausted retries because the SDK hid the finished
child sandbox. The fix recovered its exact effect and confirmed teardown using
the same shared fenced Job. Both repeat resources are confirmed stopped, its
PR/ref were cleaned, and selected-user Modal inventory reported zero active
sandboxes. Its interrupted Turn is never converted into success or approval.

## RFC A01–A31 gate matrix

PASS means the stated implemented scenario has executable evidence. PARTIAL
means some required scenarios or release drills remain unverified; these gates
are not claimed fully passed. DEFERRED is an explicitly optional later layer.
All cloud-free test paths below are under `tests/unit/unified` or
`tests/integration/postgres`; live IDs/versions are in the JSON evidence above.

| Gate | Status | Executable evidence and limits |
| --- | --- | --- |
| A01 accepted intent | PASS | `test_foundation.py`, `test_business_api.py`: committed receipt reconstruction, same IDs/body replay, changed-body rejection |
| A02 queue/admission | PASS | Foundation concurrent transactions and `test_connections.py` concurrent last-slot admission; typed uniqueness and capacity reservations |
| A03 transactional authority | PASS | Foundation rollback, immutable journal/terminal triggers and projection reducer rebuild |
| A04 claim loss/fencing | PASS | Stale claim/resource-fence rejection, stable effect IDs; delivery and I/O lost-response reclaim |
| A05 allocation ambiguity | PASS | Runtime pipeline dropped allocation response; Modal exact tagged adoption and finished-effect no-reallocation test; actual repeat teardown recovery |
| A06 native start ambiguity | PASS | Runtime journal reconstructs fsynced accepted/starting operations without replay; unresolved launch remains unknown |
| A07 cancel/success race | PASS | Parameterized execution evidence tests cover committed cancel precedence, late success and incomplete stop; process-group cancellation |
| A08 replay | PASS | Source tuple dedupe/gap/conflict tests, cumulative part revisions, native runtime spool and Console reconnect reducer |
| A09 terminal watermark | PASS | Missing final frame seals interrupted, no complete success; repeated terminal evidence remains idempotent |
| A10 pure reads | PARTIAL | Repeated owner-scoped API reads and diagnostics remain unchanged and cannot call fake executor/GitHub; exhaustive fault coverage of every file/preview GET is not recorded |
| A11 continuation | PASS | Complete real first/follow-up/checkpoint/destroy/restore preserved native ID and marker; fake mismatched native ID and unsupported versions refused |
| A12 provider honesty | PARTIAL | One enabled experimental official OpenCode lane; truthful unsupported/unknown capabilities and observed native/tool/usage normalization; complete malformed/error fixture matrix is not yet recorded |
| A13 protocol rollout | PARTIAL | Major/body/fence incompatibility tests, installed CLI/source checks, built-image recording and warm-image equality; broader negotiated minor rollout matrix remains unverified |
| A14 runtime loss | PASS | Lost execution preserves Session/history, quarantines compute and reserves capacity until confirmed isolation; interrupted repeat supplies actual unavailable-runtime evidence |
| A15 pressure | PARTIAL | Bounded spool/journal tests and reserved terminal lane diagnose output pressure; physical ENOSPC/fsync failure drills and emergency disk reserve are not complete |
| A16 checkpoint barrier | PASS | Runtime pipeline warm checkpoint and replacement restore; I/O writer barriers, active service/Turn rejection, failed/tampered capture refusal |
| A17 environment/cache | PASS (cache disabled) | Immutable Project pinning; native/private checkpoint precedence and warm preparation marker; no cache reuse or cross-Session sharing is enabled |
| A18 credential isolation | PASS for MVP lane | AES-GCM owner/AAD/keyring tests, selected-owner runtime env and actual two-user denial/secret scans; disabled providers receive no credentials |
| A19 replace/revoke | PASS for manual credentials | Version-pinned stale validation rejection, revoke/replace and live teardown dependency tests; password epochs revoke login/API/tool grants; static keys have no writeback |
| A20 capture integrity | PASS | Binary/untracked/deleted/mode/symlink canonical vectors, stage-before-swap application, tampered blob/escape denial and immutable database seals |
| A21 exact subject | PASS | Contract digest/head validation, deterministic remote full-tree verification, stale subject denial; invented head and invalid verdict rejected in earlier live runs |
| A22 Delivery retry | PASS | Lost PR response adoption survives reconstruction; private export works after author teardown; no coding Turn is rerun for delivery |
| A23 target concurrency | PARTIAL | Global ref claims, exact subject/head and independent-result tests; real draft rejection/reconcile; exhaustive concurrent remote base/check mutation drill not recorded; strict atomic base stability fails closed |
| A24 salvage | PARTIAL | Automatic capture requires succeeded source Turn and matching generation; salvage records ineligible origin; complete cancelled/failed/interrupted salvage fault matrix not recorded |
| A25 generic children | PASS for enabled lane | Ordinary isolated Session/Worktree creation with pinned ChangeSet/ResultContract, denied child shipping, explicit apply/transfer; independent real child review before merge |
| A26 waits/results | PASS | Durable wait/deadline Jobs, subtree cancellation, immutable validated result, strict correction through ordinary child Turns; no sleeping authoritative worker or invented capacity release |
| A27 files/PTY/preview | PARTIAL | Confined paths/symlinks, real supervised PTY/barrier/redaction, exact declared services and separate-origin credential-stripping HTTP proxy; exhaustive hostile preview browser/expired-grant matrix not recorded; no preview WebSocket support |
| A28 Console state | PASS | Watermark/gap/schema/reconnect dedupe, uncertain mutation key/body retry, owner/logout cache purge; real responsive login/composer/storage checks |
| A29 import | PARTIAL | Offline sanitized archival rehearsal covers conflicts, explicit ownership, digests/provenance and restart idempotence; history becomes archived notes, not a complete production-format data migration |
| A30 deletion | PASS for deployed MVP | AST boundaries/deleted-root tests, sole active `/api` and generated spec, unified SDK/CLI/deploy; legacy docs only in explicit archive, no deployed import bridge |
| A31 later extensions | DEFERRED | RFC 07/10 explicitly allow marketplace/hooks/schedules/webhooks/OAuth/BYO/recipes after core deletion; none is advertised or running as a second engine |

## Local validation and cutover disposition

- `make lint` and `make spec-check`: PASS.
- Full isolated actual-PostgreSQL backend/runtime suite: **54 passed**, no skips;
  eight FastAPI/Starlette dependency deprecation warnings.
- Console typecheck/production build and **4 tests**: PASS.
- Documentation site build and links: PASS (82 pages at the check before this report).
- Wheel build: PASS; 16 SQL migrations, unified SDK/composition root included,
  retired runtime/API namespaces absent. Compose configuration: PASS.
- `git diff --check`, frozen RFC byte equality, stacked signature/five-known-value
  secret scan and seven retained private log scans: PASS. Final receipts and CI
  URLs are in the final PR comment/local session artifact.
- Phase 5's failed CI checks were the unsupported Testing Library `exact`
  option, corrected in Phase 6; final Console typecheck verifies the correction.

R0–R5 core paths are implemented. R6 archival import is rehearsed in disposable
state, but production inventory/backup-restore/full-format migration, old live
cohort drain and activation remain operator release work; no production data
was read or mutated. R7 deletes deployed legacy authority. The A-gate partial
rows above remain explicit release limitations; this report does not certify
every normative production acceptance gate.

Deleted: old control Task/Agent/Run/Workflow stores/reducers/routers/watchers,
V1/V2/hosted review/revision machinery, old runner/image/entrypoints, broker,
`web/**`, legacy Console facades, SDK/CLI namespaces, old test/fake trees and
deployment roots. Retained: thirteen frozen RFC files, inert historical docs
under `docs/archive/legacy`, unified resource owners, official Harness/runtime,
and the offline importer outside the deployed application. No dual writes or
permanent compatibility facade remain.

The stack changes roughly 992 files, adds 17.8k lines and removes 185.5k lines
(rename-aware figures before these evidence documents). Optional Codex is
disabled; no Codex login is required. Environment cache, native MCP, interactive
approval/steer, cross-account native resume, general preview WebSockets and
later extensions remain unavailable. Public email requires an operator delivery
command. The HTTP shell tool gateway's authorization is tested; its public TLS
transport has not had a real remote CLI gate. No OCI digest or performance/cost
SLO is asserted. Encrypted local acceptance cohorts and private screenshots are
deliberately retained as evidence; disposable remote effects are cleaned.
