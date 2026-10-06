# SOR-99 independent review

Reviewer: Devin (independent; did not author the implementation)
Base: `e1fd427` · Reviewed commit: `a9df3f9` (Release 0.1 credentials: SOR-99
provider credential onboarding)
Result: **FIX** — one blocking defect found and repaired in-tree; scope
otherwise PASS.

## Scope checked

SOR-99 §1–§8: unified `ProviderDescriptor` (5 stable providers + `claude`
experimental), `add`/`import` from file/dir/blob/stdin with provider-mismatch,
symlink, path-escape, permission (0600) and per-kind schema validation; the
credential blob persists only to the `AccountStore` credential lane
(`FileAccountStore` 0600 / `modal.Dict` `credential/<id>`), the registry
record stays metadata-only; `verify` sits behind a `CredentialProbe` seam
(`StaticCredentialProbe` default + `SandboxVerifyProbe` running `runner init`
in a throwaway sandbox destroyed in `finally`); `status`/`list` are
metadata-only; `refresh` validates fully before the single atomic write so
last-good is preserved; `disable`/`enable`/`remove` with `--yes` confirmation
and running-session refusal; `auth_invalid` → `invalid` → excluded from
`auto` scheduling (`test_auth_invalid_excluded_from_auto_scheduling`).

## Blocking defect found and fixed in this review

1. **`account_id` path traversal into the file store.** Every
   `account_id`-taking entry point passed the id straight into
   `FileAccountStore` paths (`<root>/accounts/<id>.json`,
   `<root>/credentials/<id>.json`): `add --account-id ../escape` wrote the
   record outside `accounts/`, an id containing `/` (or a leading `/`, which
   resets the joined path) escaped the store root entirely, and
   `remove ../victim --yes` deleted an arbitrary `<id>.json` once
   `accounts/` existed. The same string is embedded verbatim in
   `sbx-acct-<id>` Secret names and `credential/<id>` Dict keys.
   `OnboardingService` now validates ids against
   `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (`invalid_account_id`) at every entry
   point — `add`, `refresh`, `export`, `verify`, `status`,
   `disable`/`enable`, `remove` — before any store I/O.
   Regression tests: `TestAccountIdSafety` (4 tests).

## Verified, non-blocking observations

- Real per-provider auth/model probes are honestly deferred, not faked:
  `verify` defaults to `StaticCredentialProbe` (schema-only) and the
  `SandboxVerifyProbe` seam runs `runner init` only — the module docstring
  states this plainly. Missing credentials yield `no_credential` /
  `probe_unavailable`, never a fabricated `ok`.
- `ModalDictAccountStore`/`ModalBackend` are untouched by tests; the modal
  lane lazy-imports `modal` — `make test` stays credential-free per AGENTS.md.
- The pre-existing sibling CLI `python -m control.accounts import
  --account-id` (SOR-63) carries the identical unvalidated-id traversal
  against the same `FileAccountStore`. Out of this issue's write scope —
  worth a child issue for store-level id sanitization in `FileAccountStore`.
- `test_cli_persists_to_file_store`'s leak assertion
  (`"token" not in record or "files" not in record`) is weak but not wrong;
  record shape is exercised by the stronger `status --json` checks.

## Tests run

- `pytest tests/unit/control/test_onboarding.py` — 59 passed
- `pytest tests/unit` — 971 passed; `pytest tests/integration` — 102 passed, 1 skipped
- `ruff check` + `ruff format --check` on changed files — clean
- Manual traversal reproduction (`add`/`remove` with `../` ids) — refused
  post-fix; pre-fix behavior confirmed escaping writes/deletes.
