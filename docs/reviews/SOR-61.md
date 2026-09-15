# SOR-61 independent review (Release 0.1 runtime)

Base `e1fd427` → reviewed HEAD `3060cea` (provider image family: pins, version
gates, doctor manifest). Reviewed against the SOR-61 Linear acceptance: stable
provider pins, reproducible builds, no baked credentials, `image_for` mapping,
doctor-consumable metadata, contract HOME/credential layout.

## Blocking defect found and fixed in this review commit

1. **`--version` pin gates matched substrings, so a different version could
   pass.** Both the in-image gate (`grep -F <pin>`) and the build-host gate
   (`expected not in out`) accepted any output containing the pin as a
   substring — `agy 1.2.20` or `grok 11.0.24` would have satisfied pins
   `1.2.2` / `1.0.24`, silently publishing an image on the wrong CLI. Both
   gates now match the pin as a whole version token via a shared ERE
   (`(^|[^0-9.])<pin>([^0-9.]|$)`): `cli_version_check` emits
   `grep -E '<pattern>'` for in-image build steps and
   `_assert_host_cli_version` applies the same pattern with `re.search` for
   the agy/grok host-binary check. Dockerfiles regenerated
   (`python -m runtime.image --write-dockerfile`).
   Regression tests: `test_host_cli_version_gate` (prefixed versions),
   `test_cli_version_check_gate_rejects_prefixed_versions` (runs the rendered
   shell command end-to-end against fake CLIs), `test_cli_version_check_shell_command`.

## Verified, non-blocking observations

- **Real docker verification ran on this host** (daemon available):
  `Dockerfile.local` and `Dockerfile.opencode.local` built cleanly — the
  codex `grep -E` gate passed in-image, `opencode-ai@1.18.29` was pulled from
  npm and reported `1.18.29`, `HOME=/work/home` held, and the entrypoint
  exited <5s on SIGTERM. The devin image test asserts the sha256-pinned
  bundle install + gate in the generated Dockerfile (host download path was
  already covered by SOR-74 gates).
- **`control/` does not yet resolve `opencode` → `sbx-runtime-opencode`.**
  `control.config` has no `OPENCODE_IMAGE_NAME` and
  `control/backends/modal.py::_PROVIDER_IMAGE_NAMES` maps only
  antigravity/grok, so an opencode sandbox would fall back to the codex base
  image. This predates the reviewed commit (unchanged since `e1fd427`) and
  belongs to the opencode adapter/control package (`control/` is outside
  SOR-61 ownership); the `image_for` docstring was corrected so it no longer
  claims control-plane sync for a provider that is not wired yet. Flagged as
  the follow-up integration point, not a regression.
- **Host pin drift is honestly gated, not faked.** The review host's `agy`
  reports `1.2.3` while `packages.txt` pins `agy_version=1.2.2`
  (SOR-60-verified): `make image-antigravity` now fails closed with a clear
  message. Publishing on this host requires installing the pinned binary or
  a deliberate pin bump with fresh spike evidence — correct behavior.
- **Credential layout is consistent with implementation, minor contract
  tension.** Manifest devin `credential_files` =
  `.local/share/devin/credentials.toml`, matching
  `control/backends/modal.py::_ACCOUNT_CREDENTIAL_FILE_REL`; the frozen
  `filesystem.md` layout diagram still shows `.config/devin/` (predates this
  commit; contract file is not editable from this package).
- `image_manifest()` is a pure function of `packages.txt` + `IMAGE_BUILDERS`
  — no Modal import, no network, no host probe; `providers.codex.env = {}`
  is the provider overlay (base image env already pins `HOME=/work/home`;
  the `layout` block carries the contract paths).
- `_cli_version_output` now decodes with `errors="replace"` so a CLI emitting
  non-UTF8 `--version` output produces a clean gate failure, not a traceback.
- No credentials/tokens anywhere in the diff; host-binary CLIs remain
  build-host artifacts (never committed); `make test` still performs no
  Modal calls.

## Verification run

- `make lint`: ruff check + format clean.
- `make test`: 1036 passed, 1 skipped (pre-existing placeholder skip),
  including the real docker builds above.
