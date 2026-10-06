# SOR-83 independent review

Reviewer: Devin (independent; did not author the implementation)
Base: `ec40b00` (SOR-82 review fix) · Integrated head reviewed: `b94c4d6`
Result: **FIX** — blocking correctness defects found and repaired in-tree.

## Findings fixed

1. **Manifest over-collection vs. transport** — `snapshot_workspace_artifact`
   collected every policy-allowed file, including gitignored test/build
   output and embedded-repo contents that `patch.diff`/`repo.bundle` can
   never reproduce; consumer-side `_verify_files` then always failed
   `checksum_mismatch`. Collection is now bounded to `git ls-files -co
   --exclude-standard` (tracked ∪ non-ignored untracked), and opaque
   embedded-repo boundaries join the diff pathspec exclusions so
   `git add -N -A` no longer hard-fails on a commitless nested repo.
2. **Empty patch rejected** — a no-change workspace produces an empty
   `patch.diff`; `git apply` exits 128 on empty input. The apply step is
   now skipped for empty payloads (deliberately not `--allow-empty`, which
   also swallows non-diff garbage as a silent no-op).
3. **Unrelated dirty files swept into handoff commit** — the old
   apply → `status --porcelain` → `add -A` → commit path staged any
   consumer worktree dirt. Now `git apply --index` stages only the patch;
   the commit fires only when `diff --cached` is non-empty.
4. **Test commands ran in the wrong cwd** — `cd {workdir}` was relative;
   now `cd` into the absolute sandbox path.
5. **Symlink checksums** — local `sha256_file` followed symlinks;
   now returns `None` for non-regular/symlink paths, matching remote
   collection behavior.
6. **Denylist gaps** — added `.claude`, `.sbx-handoff`, `runner.pid`,
   `netrc`, `credentials*`, `.credentials*`, `events*.jsonl` to the
   artifact denylist per the contract's stated boundaries.

## Regression coverage added

- `test_ignored_files_stay_out_of_manifest` — gitignored junk excluded;
  artifact still consumable end-to-end.
- `test_empty_patch_handoff_is_a_noop` + `test_empty_patch_is_a_noop` —
  zero-change artifact applies cleanly at both B1 and B2 layers.
- `test_embedded_repo_does_not_break_snapshot` — nested `git init`
  neither breaks snapshot nor leaks into the consumer workdir.

## Verification

- `make lint` — clean (ruff check + format).
- `make test` — 835 passed, 1 skipped (2 pre-existing deprecation warnings).
- SOR-82 behavior re-inspected: cancel-before-terminate ordering,
  `ControlPlane.close()` finalizes runs → snapshot → backend terminate,
  snapshot failures swallowed during teardown — unchanged.
