# SOR-294 independent review remediation

Acceptance contract: independent review of SBX Hosted Alpha PRs #157–#161,
`REVIEW.md`, `findings.json`, and `evidence-index.md` from the operator's protected
review directory. Reviewed baseline: `443f8aa3c85bb2a27d5a96ece74993563075bff4`.
All nine findings are confirmed and fixed; none is dismissed.

Final tested implementation: `23715e69e0c5262bdd77a5d461c531e27cba03b7`.
The earlier implementation deployment was `d5017d7b41b2066b21a3cfca6d893b8eb5bf9b9f`.
Real acceptance exposed the persisted startup state `error`; the final implementation
adds that state to REV-009 and tests the actual dispatch-failure writer.
The evidence commit changes documentation/images only. The PR body records the
exact final branch HEAD, its subsequent deployment, and CI results. Deployment
packages Git blobs from a clean, pinned commit, verifies every uploaded byte,
and writes `/var/lib/sbx-hosted/release.json`; frontend `build-manifest.json`
records its source commit. Neither publication includes working-tree bytes.

| Finding / original bug | Root-cause fix and files/functions | Regression | Real deployed acceptance |
| --- | --- | --- | --- |
| REV-001 P1: idle VPS agents permanently occupy Codex capacity; catalog disagrees | `control/hosted_lifecycle.py`: `HostedLifecycle`, `SweepStore`, `SweepCheckpoints`; lifespan wiring in `control/app.py`; owner-bound snapshots in `control/hosted_compute.py` and `control/real_modal.py`; `control/reaper.py` heals interrupted claims; `HostedScheduling.running_count` in `control/hosted_accounts.py` shares scheduler/record truth with catalog | `test_rev001_sweep_releases_capacity_preserves_history_and_recovers_owned_compute`; `test_rev001_sweep_claim_retains_concurrent_followup_and_restart` | Three real completed sessions make both Codex models busy. Two healthy sessions checkpoint/suspend, dead compute becomes terminal, and capacity becomes available. Fresh personal-key author, PR and independent native reviewer then succeed. Restored native follow-up preserves repository/history. |
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

- `make lint`: green, 544 Python files formatted.
- `make test`: **3,459 passed, 14 skipped**; 393.15 seconds.
- Targeted backend remediation cases: **20 passed**.
- Console tests in the regular test environment: **16 files, 133 passed**.
- Console TypeScript check: green.
- Hosted console production build with explicit API origin and source SHA: green.
- Chromium mobile/auth regressions plus mock coding/fix/review workflow: **3 passed**.

See [local-checks.log](logs/local-checks.log) for concise command/results and
[real-gates.log](logs/real-gates.log) for sanitized provider/runtime/lifecycle gates.
Two dependency deprecation warnings are retained in original local logs; neither
is a test failure. Raw logs stay outside Git.

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
`07-fresh-author-review.webm`. The fifth is an intermediate capture before the
startup-state correction; sixth records the corrected terminal failure/retry.
Screenshots plus sanitized logs provide the reviewable committed evidence.
