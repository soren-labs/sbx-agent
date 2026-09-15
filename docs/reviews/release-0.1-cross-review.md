# Release 0.1 cross-review

Reviewer: Devin (independent cross-review of the integrated candidate)
Scope: SOR-95 release gate across SOR-61 / SOR-96 / SOR-98 / SOR-99 /
SOR-101 / SOR-102 integration surfaces.
Result: **FIX** — bootstrap/credential/runtime/docs surfaces disagreed in
four places; all fixed in-tree with regression tests. Durable runs,
multi-account scheduling, artifact handoff and workflow recovery are
untouched and remain green (1268 passed, 1 skipped).

## Findings fixed

1. **Onboarding overstated support tiers** — `PROVIDER_DESCRIPTORS` marked
   all five release providers `stability="stable"`, so
   `sbx-onboard providers` and `describe()` printed `stable` for devin /
   antigravity / grok / opencode while the release matrix (`README.md`,
   `docs/providers.md`) says Experimental / Preview. Replaced the single
   field with `support` (release tier, mirrors docs) + `experimental`
   (import gate). `--experimental` now gates only claude. Regression tests:
   `test_support_tiers_match_the_release_matrix`,
   `test_every_release_provider_imports_without_flag`,
   `test_claude_is_unsupported_and_gated`,
   `test_cli_providers_output_uses_release_tiers`,
   `test_describe_reports_support_tier`.
2. **`image_manifest()` env diverged from the control plane** — opencode's
   advertised env was `agent_home_env()` (HOME only) while
   `control/backends/modal.py::_create_env` injects the devin-style
   HOME+XDG overlay (auth.json is an XDG data file). Manifest now uses
   `devin_runtime_env()` for opencode; `image_for` docstring updated to
   describe the real resolution. Regression test:
   `test_manifest_env_matches_control_plane_injection`.
3. **`docs/deployment.md` documented a nonexistent flag** —
   `sbx uninstall --purge`; the CLI has `--purge-data` /
   `--purge-credentials`. Fixed in deployment.md and README lifecycle text.
4. **`docs/providers.md` contradicted itself on opencode** — matrix said
   Preview while the trailing note said "not supported", and the
   credential-files table omitted opencode. Added the
   `.local/share/opencode/auth.json` row and rewrote the note: merged,
   schedulable, Preview until a real-account gate passes; claude stays
   unsupported/unschedulable.
   Plus consistency: `.env.example` gained `OPENCODE_BIN` / fake-scenario /
   `SBX_AGY_BIN` / `SBX_GROK_BIN` entries; `docs/architecture.md` and the
   README diagram list opencode; `docs/contracts/filesystem.md` devin path
   corrected to `.local/share/devin/credentials.toml`.

## Verified (no changes needed)

- **OpenCode wiring is honest**: adapter registered
  (`opencode run --format json`, `--session` resume), `ProviderId` accepts
  it, `PROVIDER_IMAGE_NAMES` → `sbx-runtime-opencode`, `SBX_OPENCODE_*`
  account envs, fake/replay coverage exists. No real-account Modal gate
  evidence — Preview is the correct claim and is now uniform.
- **Bootstrap surfaces** (`sbx init/config/status/deploy/doctor/smoke/
  upgrade/uninstall`) match `docs/bootstrap.md` / `docs/deployment.md`
  after the flag fix; teardown scopes and `finally`-delete smoke behavior
  verified against `sbx/uninstall.py` / `sbx/smoke.py`.
- **Credential surfaces**: `SBX_ACCOUNT_CREDENTIAL` blob schema, per-
  provider relative paths, `0600` files / `0700` dirs, hash-only key
  storage — consistent across `control/onboarding.py`,
  `runtime/runner/credentials.py`, `docs/providers.md` and
  `docs/contracts/filesystem.md`.
- **Security regressions real**: env scrubbing in `tests/conftest.py`,
  artifact forbidden-path + secret scans, run-error/event/SSE redaction,
  no credentials in images — all covered by the existing suites.
- **Durable behavior preserved**: run ledger, scheduler, artifact handoff,
  workflow recovery files untouched; full suite green.

## Deferred (non-blocking, per scope)

- Real-account Modal gates: opencode (SOR-96), agy/grok fleet matrix
  (SOR-68), devin `/v1` e2e. No gate claims real-account support anywhere
  in the repo.
