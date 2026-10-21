# B1 acceptance record: self-hosted control plane baseline

Date: 2026-10-21. Package: B1. Implementer: Ubuntu Claude Code (Opus 5.5 high). Status:
**accepted by Mac Codex for the documented B1 scope**. Branch
`feat/oss-b1-selfhost-20261021`, based on `12deab4` (ENV integration ref, PR #236).
No production resource used. GitHub CI remains a separate PR gate.

## Completed behaviour

| Need | Delivered |
| --- | --- |
| First run | `sbx server init`: private config `$XDG_CONFIG_HOME/sbx/server.env` (dir 0700, file 0600, `O_EXCL`, never overwritten), fresh vault keyring + runtime master key, private PostgreSQL cluster in the data dir (Unix socket only, 0700, `peer` auth, TCP rejected) or `--external-database` (URL from stdin). Refuses a data dir or database that already holds SBX data, so new keys are never paired with old sealed credentials. |
| Config checks | `sbx server check`: file mode, required keys, key formats, Resend sender rule, data dir, PostgreSQL reachability/auth/database/version/migrations, executor availability (incl. Harness CLIs on PATH for `local`), port. Secret values never printed; sources (`environment`/`file`/`default`) reported. `UnifiedConfig.from_env` now refuses malformed vault/master keys and unknown executors at start-up with the fix. |
| Start/stop/restart/status | `sbx server start|stop|restart|status|logs|outbox` around `python -m control.composition serve` (no second core). Serialized per instance (flock); a PID is ours only with matching start time, cmdline, `SBX_DATA_DIR` and `SBX_SERVER_CONFIG`; another config over the same data dir is refused and never stopped. `pg_ctl stop` failure is reported, not hidden. `sbx serve`/`migrate` also read the config file. |
| Distinct errors | Categories `config` / `reachability` / `authentication` / `executor`, exit codes 3/4/5/6, on the server side (`sbx server …`) and the client side (`sbx status`, `SBXClient.diagnose()`). `transport_error` now names the base URL and an action. |
| First account without Console | `sbx auth register`, `sbx server outbox`, `sbx auth verify` (password/token from stdin, prompt or file; never argv). |

Unchanged: OpenAPI (`/api`, `/healthz`, `/readyz`), migrations, runtime protocol, Harness
manifests, identity/API-key scopes, vault and credential grant boundaries.

## Real acceptance (isolated user data, real processes)

Run on Ubuntu 26.04 / PostgreSQL 18.6 / Python 3.12.15 with a fresh `HOME`/XDG root under
`/tmp`, all `SBX_*` variables unset, a random free port; removed afterwards. Transcript
(no secrets) is in the coordinator results file. Observed:

1. `server init` → managed cluster created, config mode 600 / dir 700; second `init` refused
   (exit 3), file hash unchanged.
2. `server check` → ok (cluster stopped = warn; `local_harness_clis` warn: only `codex`,
   `claude` on PATH).
3. `sbx status` before start → `reachability`, exit 4. `server start` → running; repeated
   start → `already_running`. `sbx status` without key → `authentication`, exit 5.
4. `auth register` → `outbox` → `auth verify` → `auth login` → `sbx status` ok
   (`usable: local`; Modal warned as having no Connection).
5. Session create with no inference Connection → `connection_required` with action.
   With a placeholder inference Connection (`REDACTED` key, `https://….invalid` host, nothing
   external contacted): Session + Turn created on the **local executor**: `executor.bound`,
   `worktree.restored`, then `turn.failed` `runtime_incompatible`/`spawn_failed` because the
   OpenCode CLI is not installed on this host. Read back via `sessions show`/`events`.
6. `server stop` → stopped, managed PostgreSQL stopped, `local_runtimes_running: 1` reported.
   `server status` exit 4; `sbx status` exit 4; client command → `transport_error` naming URL.
7. `server start` → **event journal byte-identical, Session object identical**; lease still
   `ready` (runtime adopted). `server restart` → new PID, same Session list.
8. `server check` with a wrong DB user → `authentication` exit 5 with PostgreSQL's reason;
   unreachable DB → `reachability` exit 4.
9. Password and verification token absent from `server.log`.

## Automated verification

* New: `tests/unit/test_selfhost_config.py` (config privacy/precedence, two-config isolation
  regression, validation, classification, init refusals), `tests/integration/selfhost/
  test_server_lifecycle.py` (real managed PostgreSQL + background server: first start, Session
  create/read, stop/start/restart with durable state, separate-instance isolation incl.
  `/proc/PID/environ`, DB error categories, unreachable server), `tests/integration/sdk/
  test_diagnostics.py` (diagnose categories against the real API), `test_unified_config.py`
  additions. `tests/conftest.py` also strips the new server variables.
* Ubuntu `make lint` and `make docs-check` passed. Full `make test`: **610 passed,
  1 skipped**, exit 0. The skip is the existing environment probe from the ENV baseline.
* Mac Codex independently reviewed the cumulative diff against `12deab4`, checked the
  real acceptance transcript and re-ran the real-process lifecycle and SDK diagnosis tests
  on Ubuntu: **8 passed** (`tests/integration/selfhost/test_server_lifecycle.py` and
  `tests/integration/sdk/test_diagnostics.py`). `git diff --check` passed. No reproducible
  P0/P1 finding remains in the supported B1 workflow.

## Keepalive claim regression (Mac failure recorded in ENVIRONMENT_NOTES.md)

`test_keepalive_holds_the_claim_through_a_slow_call_and_stops_after_it` was **not reproduced on
Linux**: isolated ×3, under 40 CPU-burner processes on 14 cores ×8, the Mac's targeted command ×3
(74 passed each), ENV full suite green, B1 full suite. No product change. Inspection note (P2
follow-up, not implemented): the keepalive thread stops permanently and silently on the first
renew exception, and renewal period is `lease/3` plus renew latency.

## Compatibility boundaries

* `local` executor needs Linux and the chosen Harness CLI on the control plane's PATH; it is a
  single-user development executor, not a sandbox. Modal remains the remote path (unchanged,
  not exercised: no Modal account used).
* No real model Turn was run (no inference key or OpenCode CLI used); Turn success paths are
  covered by existing fake-CLI tests only.
* Malformed `SBX_VAULT_KEYS` / `SBX_RUNTIME_MASTER_KEY` / `SBX_EXECUTORS` that previously
  started (and failed later or were ignored) now stop start-up. Production uses valid values;
  check before deploying.
* macOS: lifecycle code falls back to PID liveness (no `/proc`); not verified on macOS.
* Console, SaaS, subscription Machine Slots, resident machines (B2), MCP (B4) out of scope.

## Next steps

The keepalive observation remains a non-blocking follow-up; no defect was reproduced in
Linux acceptance, so it does not justify broadening B1. B2 can build on the config file /
data dir / `sbx status` categories; B4 can reuse `SBXClient.diagnose()` and `ClientConfig`.
Remote resource execution and real subscription Turns are still accepted under B2/B3,
and actual Codex/Claude MCP clients under B4/B5.
