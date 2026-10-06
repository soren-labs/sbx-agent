# Release 0.1 RC — independent review of the integrated candidate

Reviewer: Devin (independent RC reviewer/fixer)
Reviewed HEAD: `7aac2bb163323738e96e099273e6f68c5268d2df`
(`release/0.1-rc` — "Release 0.1 RC integration: merge
release/0.1-blocker105 + release/0.1-blocker106")
Result: **PASS — no blocker found.** No product code changed; this note is
the audit record. Worktree left clean.

## Gates re-run on this HEAD

| Command | Result |
| --- | --- |
| `uv run ruff check . --exclude spike` | clean |
| `uv run ruff format --check . --exclude spike` | 248 files formatted |
| `uv run pytest tests/unit tests/integration` | 1345 passed, 1 skipped, 2 third-party deprecation warnings |
| `make test-e2e` (Playwright, chromium installed fresh) | 11 passed |
| `python -m runtime.image --manifest` | antigravity `version`/`version_check.expect` = `1.2.3` |
| `~/.local/bin/agy --version` | `1.2.3` |

## Focused re-verification

### account_id traversal / smuggling (SOR-105)

- `control/accounts.py`: every `FileAccountStore`, `ModalDictAccountStore`,
  and `PersistentAccountRegistry` op calls `validate_account_id` before
  touching a filesystem path, Dict key (`account/<id>`,
  `credential/<id>`), or Secret name (`sbx-acct-<id>`). Regex
  `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` — fullmatch, so `../`, `%5C`,
  whitespace, leading `.`/`-`, and >128-char ids all refuse.
- Direct probe on a live `FileAccountStore`: a `..`-shaped id raised
  `ValueError` on every op and created nothing outside the store root; a
  planted record `accounts/good.json` with body `id="../victim"` decoded as
  a **disabled** `corrupt_record`, was invisible to scheduler auto-pick
  (`provider_exhausted`), and listed with `running=0`. The victim file was
  untouched.
- `/v1` (`control/api_v1/routes.py`): `_registry_account` maps the
  `ValueError` refusal to 404 `not_found` — traversal ids never 500 and
  never reach the store (covered by `TestAccountIdTraversal` against a real
  file-backed registry). Agent create with `account_id="../escape"` → 409
  `account_unavailable` with no store access.
- Scheduler (`control/scheduler.py`) filters non-conformant stored ids from
  auto-pick and refuses named lookups; `DevinAccountPool._resolve_locked`
  treats an invalid configured id as unresolvable; the reaper catches
  `ValueError` and leaves corrupt records alone; onboarding validates via
  `_check_account_id` before any read and generates only conformant ids.
- Bootstrap seeding (`control/api_v1/bootstrap.py`,
  `SBX_<PROVIDER>_ACCOUNTS` JSON / `_ACCOUNT_ID` env) flows through
  `registry.put`, which validates — a bad operator-supplied id fails the
  boot loudly rather than reaching a store path.

### Antigravity pin / contract (SOR-106)

- `runtime/packages.txt` `agy_version=1.2.3`; host binary
  `~/.local/bin/agy --version` → `1.2.3`.
- Live gate check: `_assert_host_cli_version` accepts `1.2.3` against the
  real binary and `SystemExit`s for pin `1.2.2` — fails closed both ways.
- `_version_grep_pattern` emits whole-token ERE `(^|[^0-9.])1\.2\.3([^0-9.]|$)`:
  `1.2.2`, `1.2.30`, `11.2.3` rejected; `1.2.3` accepted. (`1.2.3-rc` still
  matches — same upstream version line with a suffix tag; consistent with
  the reviewed "whole token, not prefix" design.)
- Adapter contract unchanged: `-p … --output-format stream-json`,
  `--conversation <id>` resume, top-level/fixture-nested `conversation_id`,
  string-or-object `result.error`, stale-resume detected as failure rather
  than silent fork, unknown objects → noop, malformed lines → bad-json.
- Docs/providers/tests/manifest all agree on `1.2.3`.

### Credential isolation

- `runtime/runner/credentials.py`: blob relpaths reject absolute/`..`/`\`/NUL,
  `resolve()` + `is_relative_to` containment under `$HOME` (codex
  `.codex/*` exception contained under `CODEX_HOME`), all entries validated
  before first write, files `0600` / dirs `0700`, provider mismatch fails.
- `child_env` strips `AGENT_ENV_EXCLUDE` (credential blob, CODEX_AUTH_JSON,
  Devin/Grok/OpenCode/Claude auth bridges) from every provider CLI child;
  `entrypoint.sh` additionally unsets the Devin/ACP bridge vars at PID 1.
- `events.py` `redact_*` scrubs sk-/Bearer/JWT/sbx_/xai/gh*/lin_/AKIA/AIza
  shapes before `events.raw.jsonl`/`events.jsonl`; `run_errors._SECRET_RES`
  mirrors the set on the public `run.error` path.
- `artifact_ops.credential_forbidden_values` fails artifact snapshots that
  contain the blob, individual secret fields inside it, or ambient
  credential env values — fail closed, never redact-and-ship.
- Onboarding never returns/logs credential content; `export` writes 0600
  to a non-symlink path; `remove` requires `--yes` and refuses running
  accounts; `/v1` account create never echoes the credential.
- API keys: `sbx_` tokens stored sha256-only (`InMemoryApiKeyStore`,
  bootstrap `seed`); bootstrap key printed only via `sha256:` fingerprint.

### Bootstrap / deploy safety (`sbx/`)

- `sbx deploy`: check-then-act steps; Modal auth + codex Secret preflight
  before any write; bootstrap-key rotation when a fresh local key meets a
  stale Secret (local/remote divergence fails loudly at `/v1/me` probe,
  never silently); key + basic-auth files written `0600`.
- `sbx upgrade`: durable-Dict key counts snapshotted before and after;
  shrinkage aborts.
- `sbx uninstall`: terminates app sandboxes, re-lists to prove none left,
  preserves Dicts/Secrets/keys unless `--purge-data`/`--purge-credentials`.
- `sbx doctor`/`status`/`smoke` print fingerprints and metadata only.

### Regressions (durable runs / multi-account / artifacts / workflows)

- Run ledger persists CREATING/RUNNING and terminal statuses monotonically;
  missing evidence yields explicit error/unknown, not inferred success;
  async-create failures persist and release scheduler leases.
- `AccountScheduler` keeps atomic decide/acquire/report_failure with
  external running-counts derived from the session store across restarts.
- Artifact store validates ids and member names, re-verifies size+sha256
  of every declared member on write and read; unsafe paths denied.
- Handoff validates base sha, payload checksum, commit ancestry, and
  post-apply checksums; workflow store URL-quotes caller path segments;
  cleanup rechecks session ownership before closing agents.
- Full suite + e2e green on this exact HEAD (table above).

## Known limitations (non-blocking)

- No real-account Modal gates run here (`tests/e2e_modal`, including
  `agy_gate.py`) — they need live credentials; none of the repo's claims
  depend on them. The RC image's baked agy should still be confirmed
  `1.2.3` on the deployed gate per SOR-106 follow-up.
- Local dev `/api/*` falls back to the documented `sbx`/`sbx` Basic
  credentials only when the `sbx-basic-auth` Secret envs are absent; Modal
  deploy always injects the Secret (`from_name` fails otherwise).
- `sbx deploy` cannot distinguish "remote bootstrap Secret holds a
  different token" from a generic probe failure — it fails closed at
  `/v1/me` with a remediation hint; doctor names the rotation fix.
