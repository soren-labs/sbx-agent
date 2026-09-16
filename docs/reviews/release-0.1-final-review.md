# Release 0.1 — final pre-technical-acceptance review

Reviewer: Devin (final independent release reviewer/fixer)
Reviewed HEAD: `b7d94ed33e89d88c164ed71335d562965087a39d`
(`release/0.1-gate-integration` — "Release 0.1 gate integration: reconcile
provider matrix to real gate evidence"). The `release/0.1-release-review`
worktree was `git reset --hard` to this exact SHA before any testing.
Result: **PASS — no blocker found.** No product code changed; this note is
the audit record. Worktree left clean. Per SOR-107 stop conditions: no tag,
no GitHub Release, no Owner/technical-acceptance marking.

Scope audited: SOR-95 release gate, SOR-68 real-Modal matrix, SOR-101
security gate, SOR-103 RC auditability, SOR-107 RC pipeline.

## 1. Gate evidence ↔ candidate lineage

Every real-gate record names its base as the exact successful RC deploy HEAD
`5ea5fc2` on the isolated RC plane `sbx-control-release01-rc`
(`ap-BrxZ12LH9oND05Boznp8lN`), bootstrap key by fingerprint only
(`sha256:2ba85f236e42`). Verified correspondence:

- `git merge-base` confirms the four gate branches (`gate-core`,
  `gate-agy`, `gate-grok`, `gate-workflow`) were each rooted at `5ea5fc2`,
  and `b7d94ed` is exactly `5ea5fc2` + those four branches + the matrix
  reconciliation commit. No unrelated code entered the lineage.
- `git diff 5ea5fc2 b7d94ed` product delta is only the three documented
  gate-found fixes, each exercised live on the RC plane before merge:
  - `control/service.py` — dead-sandbox follow-up → canonical
    `409 session_not_runnable` (observed live in the grok gate).
  - `control/reaper.py` + `control/config.py` + `control/modal_app.py` —
    stranded `running` record finalized `lost` past
    `turn_max_seconds + RUN_GRACE_S`; driven end-to-end by the grok gate's
    real `reap()` on a fabricated stranded record.
  - `control/api_v1/bootstrap.py` — opencode default-model pair corrected
    to ids that resolve on the account's real auth channels; the passing
    re-run used the corrected model. `control/onboarding.py` flips
    opencode's tier descriptor to match the matrix.
- Everything else in the delta is gate drivers (`tests/e2e_modal/`),
  regression tests, and docs. No gate evidence references a foreign commit.

## 2. Provider Matrix honesty

`docs/providers.md`, `README.md`, `CHANGELOG.md`, and
`control/onboarding.py::PROVIDER_DESCRIPTORS` all agree:

| Provider | Tier | Audit |
| --- | --- | --- |
| codex | Stable | Earlier real-Modal suite + committed `timings.json` stand; RC lane disclosed as **CREDENTIAL_DEFERRED** everywhere it is claimed — never recorded as a pass. See note below. |
| devin | Experimental | `/v1` gate PASS 46/46 + workflow reviewer leg; correctly below Stable (no fleet matrix). |
| antigravity | Experimental | Fleet gate PASS 50/50 with real `auth_invalid` failover; tier honest. |
| grok | Experimental | Runner lanes + fleet gate PASS; tier honest. |
| opencode | Experimental | Real gate PASS meets the SOR-96 promotion bar from Preview; seeded Zen-channel unfunded status disclosed. |
| claude | Not supported | Unregistered seam, `invalid_provider` — correct. |

Audit note (non-blocking): the evidence policy's Stable bar reads "a
passing real-account Modal E2E on the release tag", and codex's RC-tag lane
deferred on an expired ChatGPT token. The tier rests on earlier-lineage
real evidence with the deferral disclosed on every surface; the product
path itself was exercised on the RC (create → dispatch → structured
`auth_invalid` → cleanup). Recommend technical acceptance / Owner gate
re-run the codex lane after an interactive `codex login` before relying on
the Stable label in release notes.

## 3. Runtime pins ↔ built binaries

`runtime/packages.txt` ↔ `runtime/image.py` manifest ↔ host binaries:

| Pin | packages.txt | manifest `version_check.expect` | host `--version` |
| --- | --- | --- | --- |
| agy | 1.2.3 | `1.2.3` | `1.2.3` ✓ |
| grok | 1.0.24 | `1.0.24` | `grok 1.0.24` ✓ |
| codex | `@openai/codex` 0.153.0 | `codex-cli 0.153.0` | `codex-cli 0.153.0` ✓ |
| devin | 3000.10.21 (sha256-pinned bundle) | `3000.10.21` | image-internal pin — host CLI (3000.10.27) is not the baked artifact; bundle sha256s pin the image content |
| opencode | `opencode-ai` 1.18.29 | `1.18.29` | image-internal npm pin (host 1.18.15 irrelevant) |

The RC deploy record's built images carry exactly these pins; the
deployment/runtime configuration is byte-identical to the RC deploy HEAD.

## 4. Bootstrap docs ↔ commands

`docs/bootstrap.md` / `docs/deployment.md` verified against `sbx/cli.py`:
`init --profile`, `config`, `status`, `deploy`, `doctor`, `smoke`,
`upgrade`, `uninstall --purge-data/--purge-credentials` all exist with the
documented semantics; `--json`/`--config`/`--state-dir` common flags match.
The earlier `--purge` doc drift is already fixed. `.env.example` contains
placeholders only.

## 5. Secret scan (repo + history + artifacts)

- Tracked files at HEAD: pattern scan for `sbx_` keys, `sk-`, `xai-`,
  `ya29.`/JWT, `ghp_`/`github_pat_`, `lin_api_`, `AKIA`, `AIza`,
  private-key blocks → only intentional test canaries
  (`sk-canary-REDACTED-*`, `sbx_0123…`, `AKIAIOSFODNN7EXAMPLE`).
- Full `git log -p` history scan for the same shapes plus JSON
  `*_token`/`api_key`/`password`/`secret` fields → only canaries
  (`sk-THISLEAKEDVALUE12`); zero real credential material in history.
- Committed artifacts (`tests/e2e_modal/artifacts/timings.json`,
  `spike/**/out/*.json`, `spike/fixtures/real_events.jsonl`) → usage/timing
  metadata only. Gate artifacts stay gitignored; `auth.json`, `.env*`,
  `.modal.toml`, `*.pem` are ignored patterns.
- Gate drivers mint/read keys from env or `0600` files, add minted tokens
  to the leak watchlist, and never persist values — consistent with every
  gate record's `leak=0` claim.

## 6. Gates re-run on this exact HEAD

| Command | Result |
| --- | --- |
| `make lint` (ruff check + format --check) | clean, 260 files |
| `make test` (`tests/unit tests/integration`) | **1359 passed, 1 skipped**, 2 third-party deprecation warnings — includes `tests/integration/{security,recovery}`, cloud_free isolation, credential scoping, RC env isolation, run durability, workflow recovery, handoff |
| `make test-e2e` (Playwright → mock_api + local control) | **11 passed** |
| `uv run python -m runtime.image --manifest` | pins per §3 |
| Host binaries | agy `1.2.3`, grok `1.0.24`, codex `0.153.0` |
| `modal.Sandbox.list()` | **0 live sandboxes** — zero orphans |

## 7. Cleanup scope & RC Modal leftovers

- Cleanup is scoped: gate sweeps are ownership-keyed, `sbx uninstall`
  terminates only the configured app's sandboxes and only purges the
  configured Secret prefix / Dicts behind explicit flags; workflow cleanup
  re-verifies session ownership.
- Live workspace audit (profile `sorenlab2026`): **zero sandboxes**. The
  `sbx-rc-*` resources match the documented intentional set exactly —
  `sbx-control-release01-rc` + `sbx-rc-image*` apps, six `sbx-rc-*` Dicts,
  Secrets `sbx-rc-{codex-auth,basic-auth,v1-bootstrap}` +
  `sbx-rc-acct-{devin-1,opencode-1,grok-1,grok-2,antigravity-1..4}`.
  Production `sbx-control` and `sbx-*` Secrets remain intact, as recorded.
- Observation (non-blocking): pre-RC idle apps (`sbx-spike*`, `sbx-agy-gate`,
  `sbx-grok-gate`, `sbx-runtime`, `sbx-control`) remain deployed with 0
  tasks. They predate the RC pipeline and are runner-gate/spike deployment
  vehicles, not RC leftovers; they hold no compute. Deleting them is out of
  scope for this scoped review — flagged for Owner cleanup discretion.

## 8. Durable semantics

Unchanged and green: run ledger monotone terminal truth (no inferred
success), async-create lease release, atomic scheduler decide/acquire/
report_failure with restart-derived running counts, artifact
size+sha256 re-verification and fail-closed secret scan, handoff
base/ancestry/checksum validation, workflow recovery from
`API key + workflow_id` alone, scoped workflow cleanup. The workflow gate
proved this end-to-end on the RC plane twice (client kill mid-SSE →
`recover()` → replay/resume → redeploy durability).

## Known limitations carried into acceptance

- Codex RC lane CREDENTIAL_DEFERRED (external stale token; see §2).
- The RC plane still serves the rc-deploy build; the three gate fixes land
  on the next deploy — recorded in the gate docs.
- Stranded-`running` reaper bounds the slot leak (20 min window) but does
  not migrate the watcher or finalize the open ledger record — bounded,
  documented in the grok gate record.
- SSE `Last-Event-ID` resume flake seen once under sibling contention;
  terminal truth was unaffected (documented transient, possible follow-up).

## Stop condition

This review concludes `release/0.1-gate-integration` @ `b7d94ed` is
consistent, honest, and green for hand-off to independent technical
acceptance. No tag, release, or acceptance verdict is recorded here.
