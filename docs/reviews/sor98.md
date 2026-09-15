# SOR-98 independent review (Release 0.1 bootstrap)

Base `e1fd427` → review of `8055572` (`sbx` CLI: init/config/status/deploy/
doctor/smoke/upgrade/uninstall). Reviewed against the SOR-98 must-delivers:
single config source, hash-only `sbx_` bootstrap key, idempotent deploy with
actionable errors, secret-safe doctor, durable-state-preserving upgrade, and
scoped uninstall.

Verified live, read-only: `ModalPlane.workspace`/`list_secret_names`/
`has_dict`/`dict_len`/`app_url`/`list_sandboxes` against the real workspace
(`sorenlab2026`), `modal app list --json` field names (`description`,
`app_id`, `state`), Modal 1.5.5 SDK signatures, and a full `sbx doctor` run
against the deployed app. Clean-env (isolated HOME/XDG) `init`/`config`/
`status`/`doctor` degrade correctly with actionable hints.

## Defects found and fixed in this review commit

1. **Basic-auth Secret used an env name the control plane never reads.**
   `sbx deploy` wrote `SBX_BASIC_PASSWORD` into `sbx-basic-auth`, but
   `basic_credentials()` only reads `SBX_BASIC_PASS`/`SBX_API_PASSWORD` —
   the board would silently fall back to `sbx`/`sbx` and the locally saved
   `basic-auth.json` would not match anything. Deploy now writes
   `SBX_BASIC_PASS`; `basic_credentials()` additionally accepts
   `SBX_BASIC_PASSWORD` so Secrets created from the pre-0.1 docs (which used
   that name) still work; the README example is corrected.
   Regression tests: `test_basic_secret_uses_env_names_control_reads`,
   `test_legacy_basic_password_env_still_read`.

2. **Doctor read non-existent `/v1` fields.** `/v1/me` returns `key_id` and
   `/v1/models` returns `accounts_available`; doctor read `id` and
   `free_accounts`/`accounts`, so a healthy deployment reported
   `key ? scopes=…` and `provider:None`. The mock transport was updated to
   the real response shape so this stays covered.
   Regression test: `test_doctor_reads_contract_fields`.

3. **`ModalPlane.ensure_dict` always reported "created".** The deploy step
   therefore claimed all four durable Dicts were created on every rerun —
   misleading idempotency output. It is now check-then-act.
   Regression tests: `test_ensure_dict_*` in `test_plane.py`.

4. **Stopped apps still looked deployed.** `app_url`/`stop_app` matched
   `modal app list` entries without checking `state`, so a stopped app kept
   producing a URL and counted as "was running". Both now require
   `state == "deployed"` (missing field → treated as deployed for older CLIs).
   Regression tests: `test_app_url_*`, `test_stop_app_only_stops_deployed`.

5. **`doctor` ignored the deploy record for the API URL.** `status`
   resolves `api_base_url or deploy.json app_url`; doctor only used the
   config value, so a deployment made under a since-moved config reported
   "api.base_url is not configured" while the app was live. Doctor now uses
   the same fallback.
   Regression test: `test_doctor_falls_back_to_deploy_state_url`.

6. **Minor hardening.** `basic-auth.json` is now force-chmod'd 0600 even
   when overwriting a pre-existing file; `sbx smoke` with an empty
   `deploy.providers` fails with `config_missing` instead of `IndexError`.
   Regression test: `test_smoke_no_providers_is_actionable`.

## Checked and not issues

- Bootstrap key lifecycle (mint → 0600 file → `SBX_V1_BOOTSTRAP_KEY` Secret →
  control stores sha256 only) is honest; stale-remote rotation covered.
- Deploy preflight (Modal auth → `sbx-codex-auth` Secret) runs before any
  write, so missing prerequisites leave zero resources behind.
- `upgrade` N→N+1 simulation and durable-loss detection are tested.
- `uninstall` re-lists sandboxes and fails loudly on leftovers; default
  scope preserves Dicts/Secrets/local key. `--purge-credentials` also
  removes `deploy.json`; docs updated to say so.
- No secrets printed anywhere: doctor/status show `sha256:` fingerprints;
  errors carry `code`+`hint`; no credential leakage in tests/fixtures.
- `examples/sbx_client.py` consumes `SBX_BASE_URL`/`SBX_API_KEY` exactly as
  `deploy` prints them.

## Known honest limitations (not faked)

- Live `/v1` verification needs a real deploy; this review verified the
  Modal-plane reads and degraded-mode UX only — `sbx deploy`/`smoke`
  against the real workspace were not run (would redeploy `sbx-control`).
- The currently deployed `sbx-control` answers `/v1` with HTTP 404 (older
  build without the `/v1` routes); doctor reports this as WARN, correctly.
