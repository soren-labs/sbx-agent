# SOR-294 independent review remediation

Acceptance contract: independent review of SBX Hosted Alpha PRs #157–#161,
`REVIEW.md`, `findings.json`, and `evidence-index.md` from the operator's protected
review directory. Reviewed baseline: `443f8aa3c85bb2a27d5a96ece74993563075bff4`.
All nine findings are confirmed and fixed; none is dismissed.

Original nine-finding implementation: `2adff83b63802edf16c36ebefe2522bd3ed771a4`.
Final-blocker implementation tested/deployed below: `7b6ac5c73de4416b9f8c39d18783a0aa65b38a5f`.
The earlier implementation deployment was `d5017d7b41b2066b21a3cfca6d893b8eb5bf9b9f`.
Real acceptance exposed the persisted startup state `error`; the final implementation
adds that state to REV-009 and tests the actual dispatch-failure writer.
Initial PR CI also exposed an actual lifecycle startup race. The final product
adds fresh durable ownership reads after remote handle enumeration and rejects
stale idle termination after a new turn; five deterministic cases cover both
windows. See [CI race reproduction and fix](logs/ci-lifecycle-race.log).
The evidence commit changes documentation/images only. The PR body records the
exact final branch HEAD, its subsequent deployment, and CI results. Deployment
packages Git blobs from a clean, pinned commit, verifies every uploaded byte,
and writes `/var/lib/sbx-hosted/release.json`; frontend `build-manifest.json`
records its source commit. Neither publication includes working-tree bytes.

| Finding / original bug | Root-cause fix and files/functions | Regression | Real deployed acceptance |
| --- | --- | --- | --- |
| REV-001 P1: idle VPS agents permanently occupy Codex capacity; catalog disagrees | `control/hosted_lifecycle.py`: `HostedLifecycle`, `SweepStore`, `SweepCheckpoints`, `SweepBackend`; lifespan wiring in `control/app.py`; owner-bound snapshots in `control/hosted_compute.py` and `control/real_modal.py`; `control/reaper.py` heals interrupted claims; `HostedScheduling.running_count` in `control/hosted_accounts.py` shares scheduler/record truth with catalog | `test_rev001_sweep_releases_capacity_preserves_history_and_recovers_owned_compute`; `test_rev001_sweep_claim_retains_concurrent_followup_and_restart`; `test_rev001_create_after_sweep_listing_is_not_orphan_killed` (four states); `test_rev001_new_turn_supersedes_stale_idle_termination` | Three real completed sessions make both Codex models busy. Two healthy sessions checkpoint/suspend, dead compute becomes terminal, and capacity becomes available. Fresh personal-key author, PR and independent native reviewer then succeed. Restored native follow-up preserves repository/history. Final race-fixed deployment also completes a fresh native session and returns 200 for its retained direct connection. |
| REV-002 P2: connected Codex disabled in picker | `control/config.py:execution_providers` shared by provider/models routes in `control/api_v1/routes.py` and resolver in `control/tasks.py`; rollout explicitly enables Codex | `test_rev002_hosted_picker_and_execution_share_provider_truth` | Production baseline disabled despite two available models; deployed browser selects Codex ready and both models. Explicit Codex execution succeeds. |
| REV-003 P1: transport/wait failures skip native cache cleanup; stale callback races new lease | `control/codex_process.py:AccessOnlyProcess`; `control/sandbox_io.py:drain`; `control/hosted_github.py:GitHubScopedBackend.exec`; sandbox-owned flock/generation/finally in `runtime/runner/access_scope.py`; runner entry wraps operation in `runtime/runner/main.py` | `test_rev003_operation_cleans_on_transport_wait_cancel_and_close`; `test_rev003_sandbox_finally_and_stale_cleanup_cannot_delete_new_lease` | Native caches absent after actual Codex. Real Modal guard checks pass for exit 0/7, stdout failure, wait failure, cancel and stale callback while newer operation holds cache. Official VPS native refresh and unauthorized-operation retry advance canonical credential version, without importing an old cache. |
| REV-004 P2: existing Ready Modal image never upgrades | `runtime/build_identity.py:runtime_build_identity`; `control/modal_connection.py:ModalConnectionService.provision`; `control/real_modal.py:publish_runtime`; worker reconciliation persists immutable desired identity and switches only after smoke | `test_rev004_ready_upgrade_smokes_before_switch_and_keeps_existing_handle` (upgrade, identical reuse, smoke failure) | Previously Ready runtime automatically republishes/smokes reviewed build. Changed non-secret build input republishes/smokes while an existing sandbox remains alive. Canonical build restored, identical build reused without metadata writes. |
| REV-005 P2: dirty/untracked rollout source uploaded | `deploy/hosted/rollout.py:reviewed_commit`, `source_archive`, `main`; clean preflight before remote writes, commit-object packaging, recheck HEAD, separate release directory + atomic activation + byte manifest | `test_rev005_rollout_refuses_dirty_source_before_remote_writes` (tracked edit, deletion, untracked file; exact pinned bytes) | Actual clean rollout verifies uploaded manifest against the implementation SHA. Dirty-source regressions make zero remote writes. Existing service/database/tunnel and sing-box PID preserved. |
| REV-006 P3: successful `/auth` login goes to missing page | `console/src/hosted/AuthGate.tsx:authReturnPath`, `AuthGate`; backend native auth page uses same safe route validation in `control/hosted_auth.js` | console hosted-remediation login/return-path/direct-route/unsafe-route cases; mobile Chromium auth redirect | Agent Browser logs in at deployed `/auth?returnTo=%2F`, reaches the valid Session workspace. Safe internal return targets and unsafe/unknown targets covered locally. |
| REV-007 P2: mobile API-key form overflows | `console/src/hosted/ApiKeys.tsx` form class; `console/src/prototype/prototype.css` stacks mobile controls, min-width/box-sizing and 44px touch size | `test_auth_redirect_and_mobile_api_key_form` at 390 keyboard / 320 touch; console keyboard create/revoke | Deployed 390×844 and 320×740: document scrollWidth equals viewport; every form control stays within viewport and is 44px tall. Keyboard/touch creation, secret concealment and revoke work. 390 key authorizes a real native author; revocation enforced afterward. |
| REV-008 P1: optimistic 202 disappears on dead retained compute | `control/api_v2/routes.py:post_message`: durable numbered intent before ACK, current ledger status on replay; `control/service.py:post_message`, `drain_queued`, `_dispatch_turn`, `reconcile_turns`: durable failure and restart drain, preserving synchronous refusal semantics | `test_rev008_late_dead_compute_failure_keeps_prompt_run_and_replay`; `test_rev008_accepted_queue_survives_missing_worker_and_cancel`; existing ACK-latency/cancel suites | Actual retained compute terminated. ACK names run 3 queued; history reaches failed `runtime_error`, prompt remains, replay returns same failed run, conflicting prompt returns 409. Control restart followed by suspended-session follow-up completes with durable numbered ACK and replay. |
| REV-009 P2: failed unbound reviewer reported running forever | `control/hosted_reviews.py:review_status`: persisted error/failed/cancelled wins before missing binding, safe error + retryable | `test_rev009_unbound_review_terminal_state_beats_missing_agent` calls `_mark_dispatch_failed` for provider/workspace/Modal startup errors; console failed-review retry case | Full capacity causes real `provider_exhausted` before agent bind. Session API and review API both report terminal failure. UI shows “Review failed — retry with a new review Session”; browser retry completes independent approval. |

## Local gates

Development/test processes use isolated HOME/XDG and a stripped environment.
No cloud credentials are required by the backend suite; 14 opt-in/environmental
tests skip. Frozen contracts/Protocol files have no diff.

- `make lint`: green, 545 Python files formatted.
- `make test`: **3,464 passed, 14 skipped**; 403.98 seconds.
- Targeted backend remediation cases: **25 passed**; broader lifecycle/durability/ownership checks **64 passed**.
- Console tests in the regular test environment: **16 files, 133 passed**.
- Console TypeScript check: green.
- Hosted console production build with explicit API origin and source SHA: green.
- Chromium mobile/auth regressions, mock coding/fix/review workflow and SQLite hosted Alpha gate: **4 passed, 1 skipped** (optional PostgreSQL browser gate).

See [local-checks.log](logs/local-checks.log) for concise command/results and
[real-gates.log](logs/real-gates.log) for sanitized provider/runtime/lifecycle gates.
The full run reports two dependency deprecation warnings and one background-thread
warning from the existing deliberately corrupt-record fixture
(`test_missing_evidence.py`, `garbage` field). All tests pass; this fixture warning
is separate from the repaired lifecycle race. Raw logs stay outside Git.

## Production verification notes

Verified real HTTPS origins: `https://sbx-agent.com` and
`https://api.sbx-agent.com`; existing Cloudflare Pages project/tunnel, user-owned
Modal workspace and GitHub App remain in place. The API uses local PostgreSQL
peer authentication, one VPS worker, and the installed lifecycle worker.
The public 443 service remains sing-box with **PID 2224** throughout rollout.
`sbx-hosted`, `sbx-cloudflared`, PostgreSQL and sing-box remain active.

Disposable workflow repository only: `soren-labs/sbx-e2e-test`.
Author PRs [#13](https://github.com/soren-labs/sbx-e2e-test/pull/13) and
[#14](https://github.com/soren-labs/sbx-e2e-test/pull/14) remain drafts; the
remediation PR and original alpha PRs are not merged by this session.

The initial three real native sessions were `sess_8cb0f7591df544c4`,
`sess_0e8d36bb05454978`, `sess_0e3b65f525574bba`. The last deliberately killed
session acknowledged run 3, then durably failed; run 2 in an earlier probe
completed normally because that probe had not terminated compute. Only the
confirmed terminated-compute probe is claimed as REV-008 evidence.
The fresh author after cleanup is `sess_ed2a937a4efd45e4`.
Final race-fixed deployment native startup/direct-connection acceptance is
`sess_ba24dce5a049485c`, with finished run 1 and HTTP 200 direct connection.
The initially failed unbound reviewer is `sess_5244741f6ff34bb6`;
the successful browser retry is `sess_13cff0b224864107`.
The fresh author's independent reviewer is `sess_9f89ac88c2c64ef3`, which approves
PR #14 after the recovered author's native tests and real App delivery.

Native authorization uses the current encrypted VPS grant. No development cache,
old reviewer cache, or stale bootstrap was imported. Refresh/retry checks use
the official native client. Access caches in the sandbox contain no refresh
grant and are removed at operation end. Cleanup fault probes use `REDACTED`
placeholder files in a separate temporary probe directory inside real compute.
The deliberately rejected broker operation triggers real refresh/retry; it does
not revoke or damage the actual reusable account.

Cross-user acceptance uses a new disposable production user/key: all five
foreign session/history/connect/review/delivery requests return 404 and its
session listing is empty. Its key is revoked after the check. The mobile-created
390 key is used for actual execution and subsequently revoked; 320 key is revoked
through touch UI. Production journal and event checks compare against current
credential values privately and print only PASS; evidence is separately scanned
with Gitleaks and private-value checks before commit.

## Agent Browser artifacts

Agent Browser is used against the deployed UI, including visible input/click,
keyboard and touch interactions. One-time API-key fields are concealed before
creation/recording; secret values are captured only into protected local memory
state and never appear in screenshots/logs. Login password fields are masked.
No video, browser storage, private state or auth cache is committed.

- [Auth redirect](screenshots/auth-redirect.png)
- [Connected Codex/provider picker](screenshots/provider-picker.png)
- [Live author output](screenshots/author-live.png)
- [Mobile API key 390px](screenshots/mobile-api-key-390.png)
- [Mobile API key 320px](screenshots/mobile-api-key-320.png)
- [Terminal review failure and retry action](screenshots/review-failure.png)
- [Independent review recovery](screenshots/review-recovery.png)
- [Fresh author PR after lifecycle cleanup](screenshots/fresh-author-pr.png)
- [Fresh independent review](screenshots/fresh-review.png)

Local WebMs under `/home/zheng/.local/state/sbx-sor294-remediation/videos/`:
`01-auth-picker.webm`, `02-author-review.webm`, `03-mobile-390.webm`,
`04-mobile-320.webm`, `05-review-recovery.webm`, `06-failure-review-retry.webm`,
`07-fresh-author-review.webm`, `08-lifecycle-startup-recovery.webm`.
The eighth records the final product auth redirect, enabled Codex/model selection
and real native Session output after the CI race repair. The fifth is an intermediate capture before the
startup-state correction; sixth records the corrected terminal failure/retry.
Screenshots plus sanitized logs provide the reviewable committed evidence.


## Final independent-review blockers — FINAL-001 and FINAL-002

The independent final review at `4e8836e5e334673c1092d59e04472e0eb34788e0`
confirmed all REV-001..009 fixed and rejected two additional P2 defects.
Both are now fixed. This implementation still requires a **fresh independent
review of PR #162**; the disposable native workflow review is a different gate.
The same remediation branch/PR is used; no PR was merged or replaced.

| Finding / reproduced bug | Root-cause fix | Regression | Real deployed acceptance |
| --- | --- | --- | --- |
| FINAL-001 P2: explicit effort makes 11 Modal tags, exceeding SDK's limit of 10 | `control/modal_tags.py:modal_tags` allows the eight authority/binding tags only; `HostedModalBackend._create/list` persist/rejoin full execution metadata in owner records and share the policy across create, snapshot restore and reattach; `RealModalProvider.create/list` apply the provider boundary. CPU/memory still reach SDK sizing arguments. Ownership, session, provider/account, connection, workspace and immutable image identity remain intact. No REV-001 sweep/race guards were removed. | `test_final001_real_sdk_boundary_limits_tags_with_execution_and_recovery`: all seven explicit efforts × create/restore × two owners; configured CPU/memory, resources, recovery and lifecycle tags. `test_final001_hosted_create_restore_and_reattach_keep_durable_metadata`: all seven efforts, durable metadata and foreign-owner restore/poll/list denial. 35 cases. | Real Codex gpt-6.1-sol / low author starts, runs three unittests, then a real owned checkpoint restores after the VPS restart and completes run 3. Both original and restored real Modal sandboxes carry exactly eight identity tags; effort/surface remain durable and absent externally. Native access caches are absent after completion. |
| FINAL-002 P2: automatic publish uses legacy ambient credentials and base HEAD | `control/hosted_delivery.py:owner_revision_service/HostedAutoDelivery` is shared with explicit `get_revisions`; `ControlPlane._auto_publish_git`, `control/app.py` and `HostedLifecycle.sweep` route/reconcile durable owner/task/workspace policy + captured ready revision + successful numbered ledger. No ambient PAT fallback. `RevisionService.deliver` reloads under the shared delivery lock; fulfilled automatic intent preserves later explicit PR settings. | `test_final002_auto_publish_owner_app_captures_uncommitted_work_without_ambient` verifies actual dirty changes, owner App branch/diff/draft PR, explicit replay and draft→ready settings surviving automatic replay. `test_final002_reconstruction_recovers_crash_after_upstream_pr_before_record` reopens the durable app and converges on one PR. `test_final002_two_owner_installations_and_foreign_repo_fail_closed` verifies separate installations/tokens/repositories and foreign API/metadata denial. | Automatic real owner-App delivery creates draft [sbx-e2e-test #16](https://github.com/soren-labs/sbx-e2e-test/pull/16) with no manual /deliver call and no ambient GitHub credentials. Native files remain uncommitted and sandbox HEAD equals base; payload delivery publishes their actual diff. A native follow-up removes test-generated bytecode and automatically updates the same PR to exactly the two source files. Reconstructed workers and the restarted service retain one PR and the same payload SHA. Independent real Codex review approves that SHA. Five real foreign-owner paths return 404; temporary key revoked. |

### Final local gates

- Pre-fix focused reproductions: **11 failures** (tag-limit and automatic-delivery cases).
- `make lint`: PASS, **548** formatted Python files.
- Full isolated, credential-free `make test`: **3,502 passed / 14 skipped**, two dependency deprecations, **410.46s**, exit 0.
- FINAL-001/002 plus REV/lifecycle/reaper/ACK regressions: **106 passed**; extended revision/durability/workflow selection: **93 passed**. The new blocker file contains **38 cases**.
- Console regular test environment: **16 files / 133 tests passed**; typecheck PASS; hosted production build PASS.
- Local Chromium mobile/auth + coding/fix/review workflow + SQLite Alpha gate: **4 passed / 1 skipped** (opt-in PostgreSQL browser fixture).
- Frozen contracts and Protocol paths: zero diff. Staged implementation Gitleaks: PASS.

One full run had 3,501 passes and an unchanged timing assertion fail at 5.11s
(limit 5s). The isolated case passed in 1.15s, then the entire suite passed
without concurrent test jobs. No assertion was relaxed or skipped.
The initial browser workflow exposed automatic replay resetting an explicitly
published PR to draft; fulfilled automatic intent now preserves that edit,
with regression and full browser-loop PASS. See
[final-blockers-local.log](logs/final-blockers-local.log).

### Real acceptance and exact provenance

Tested and deployed product source: `7b6ac5c73de4416b9f8c39d18783a0aa65b38a5f`.
The committed-byte backend manifest verifies **288 source files**; public
frontend manifest verifies the same SHA and all **10 deployed file hashes**.
Primary frontend asset remains
`e3931b0bb5c362c4e030d74364d3d20a560e6573457c33417f665daf8e414b36`
(the frontend code did not change). Canonical runtime build remains unchanged.
The subsequent evidence-only HEAD is also deployed with exact manifests; its
full SHA and CI links are recorded in the PR body, avoiding a self-referential
commit hash inside this file.

Real author: `sess_5c22e526d01246d7`, explicit `low` / `gpt-6.1-sol`.
Real independent disposable reviewer: `sess_62789c530d0a47ef`, verdict approve.
Initial automatic payload `5e967028ef1d4a90cf95e51e1ad7d70484650ac8` captured
both source files and two unittest-generated bytecode files. Codex's uncommitted
cleanup follow-up runs `python -B -m unittest test_final_blockers_math.py`,
passes three tests and updates the same PR to only `final_blockers_math.py` and
`test_final_blockers_math.py`, payload
`3b3e8db618e8d1f81bace4e466750032c916d637`. Restart/restored run 3 keeps that
payload and one PR; ACK/replay names the same terminal successful run.
The sandbox base HEAD remains unchanged throughout. No ambient
GH_TOKEN/GITHUB_TOKEN/SBX_GITHUB_EPHEMERAL was enabled in the service.
The App installation selected through the durable task owner remains the
repository authority. Two-owner installation separation is covered
explicitly in deterministic tests; real foreign-owner access is independently
rechecked against this author/reviewer.

Only this disposable author's retention was expired to induce the real automatic
checkpoint/reap. Its durable suspended state and owned checkpoint were verified
before native restored execution. Hosted service restart preserves PostgreSQL,
canonical grant and prior delivery. All four services remain active; sing-box
retains **PID 2224 on public TCP/UDP 443**. No Cloudflare tunnel configuration,
chat-on-steroids resource, shared App installation or prior PR was modified.
PR #16 is closed and only its exact temporary `sbx/51f0d0a1e0eb4fc1875e7ffb18e0ea09`
branch is deleted after acceptance. Historical session/review records remain.
Current canonical native event/journal private-value leak checks both PASS;
no implementation-agent auth cache was read or modified.

See [final-blockers-real.log](logs/final-blockers-real.log). Initial browser
harness retries waited for the actual loaded Codex option and visible Review
button; Cloudflare's index 308 is followed for hash verification. A first probe
incorrectly expected suspended records to clear their historical sandbox ID;
the corrected probe checks suspended state plus the durable checkpoint. These
harness attempts are not counted as passing gates. The original uncommitted
native artifact faithfully included generated bytecode; its two-file cleanup
is a native follow-up, not an out-of-band source edit.

### Deployed Agent Browser evidence

- [Explicit low effort and automatic draft configuration](screenshots/explicit-effort.png)
- [Live native work at low effort](screenshots/explicit-effort-live.png)
- [Automatic owner-App PR delivery](screenshots/automatic-owner-app-delivery.png)
- [Independent review passed](screenshots/automatic-owner-app-review.png)

Additional accepted local WebMs in the required external videos directory:
`09-explicit-effort-startup.webm`, `10-automatic-owner-app-delivery.webm`,
`11-automatic-owner-app-review-start.webm`,
`12-automatic-owner-app-review.webm`. Short recordings finish successfully;
codec/duration/size validation is in the real log. The two `*-harness-incomplete`
files are retained locally but excluded from accepted recordings. Prior desktop
redirect/picker, 390/320 mobile, failure/retry and lifecycle recordings remain
indexed above. No secrets appear in committed screenshots or logs.
