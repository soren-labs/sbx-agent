# Release 0.1 — real Modal Grok fleet gate (SOR-68 grok lanes)

Operator: Devin (gate author)
Worktree: `/home/zheng/ai-work/p21/rel01-gate-grok`, branch
`release/0.1-gate-grok`, reset to the exact `release/0.1-rc-deploy` HEAD
`5ea5fc2` ("Release 0.1 RC deploy: isolated RC namespace + remote
PYTHONPATH fix") before any testing.
Plane under test: the dedicated RC deployment only — Modal app
`sbx-control-release01-rc`, endpoint
`https://sorenlab2026--sbx-control-release01-rc-fastapi-app.modal.run`,
RC namespace `sbx-rc-*` (SOR-107 record). Production `sbx-control` was
never touched. Auth: bootstrap key fingerprint
`sha256[:12]=2ba85f236e42` (no credential value was printed, logged, or
committed at any step).

Fleet: `grok-1` + `grok-2`, provider `grok`, model `grok-4.6`, `active`,
`max_concurrent=1` each. `grok-2` was onboarded to the RC namespace for
this gate: Secret `sbx-rc-acct-grok-2` was created from the existing
production `grok-2` credential blob (metadata only — blob shape
`{provider: grok, files: {.grok/auth.json}}`, contents never read into
logs) and the RC deploy overlay seeds both accounts at one slot via
`SBX_GROK_ACCOUNTS`.

Harnesses:

- `tests/e2e_modal/grok_fleet_gate.py` — the /v1 fleet gate: dedicated
  API key (the per-key `concurrency_limit` cap makes the shared
  bootstrap key nondeterministic under sibling gates), fleet upsert,
  per-account `/v1/accounts/{id}/verify`, `account_id=auto`
  distribution, exhaustion, named-account errors, running counts,
  two-turn resume with marker recall, control-plane restart (key-store
  cycle detection, running-count survival, post-restart resume),
  cooldown failover, out-of-band sandbox kill normalization, cancel
  error shape, closed/unknown-agent run refusal, a scoped stranded
  `running`-record reaper check, per-agent terminal cleanup, a
  my-keys-scoped leftover sandbox scan, and a leak scan over every
  captured response plus the RC app log tail.
- `tests/e2e_modal/grok_gate.py` — runner-level lane per account:
  binary/auth probe, `runner init --provider grok`, credential file
  mode `600` + dir `700`, env scrub (no `GROK_*`/`XAI_*` leak to child
  processes), two-turn resume on one native thread, corrupted native
  session → exit `2` + `codex_error` doc + no `thread.started`/
  `turn.completed` + empty stdout, credential export hash-stability,
  leak scan, sandbox cleanup.

## Verdicts

| Lane | Verdict | Checks | Notes |
| --- | --- | --- | --- |
| `grok-1` runner | **PASS** | all green | auth probe, two-turn resume, stale-session normalization, perms, env scrub, leaks |
| `grok-2` runner | **PASS** | all green | same lane, second real credential |
| fleet gate | **PASS** | all green | both accounts verified `active`; auto picked `['grok-2','grok-1']`; `provider_exhausted` at capacity |

`credential_deferred` is empty — both real credentials authenticate.

## Fleet gate detail (final PASS run)

- `fleet.shape`: `{"grok-1": active/1, "grok-2": active/1}`.
- `verify.grok-1`, `verify.grok-2`: HTTP 200, `status=active`,
  `last_error=None` — runner-init auth probe against the real stored
  Secret for each account.
- `auto.distribution`: two `account_id=auto` creates landed one per
  account — `picked=['grok-2','grok-1']`.
- `auto.exhausted_429`: third auto create → `429 provider_exhausted`
  with `retry_after: 60.0`.
- `named.busy_409`: named create on the occupied account →
  `409 account_busy`.
- `named.unavailable_409`: named create on `grok-missing` →
  `409 account_unavailable`.
- `slots.running_counts`: `{'grok-1': 1, 'grok-2': 1}` while both
  slots held — session-derived counts exact.
- `run1.finished.grok-1` / `run1.finished.grok-2`: both `FINISHED` —
  turn-level auth proof per account.
- Two-turn resume on one agent: run-2 stored the marker, run-3
  `FINISHED` and recalled it — same native thread.
- Restart (`modal deploy` identical overlay): deploy rc 0, `/v1/me`
  200, dedicated key correctly `401` on the cycled in-memory store
  then re-minted, running counts still `1/1`, auto create still
  `provider_exhausted`, post-restart run-4 `FINISHED` with marker
  recall — resume survives control-plane cutover.
- Cooldown/failover: `grok-1` set `cooling` via the accounts Dict →
  named create `409 account_unavailable`; auto failed over to
  `grok-2`, run `FINISHED`; account restored `active`,
  `cooldown_until` cleared.
- Stale normalization: sandbox terminated out-of-band → follow-up
  `409 session_not_runnable` (was a bare `500` before the fix below);
  session finalized `lost`.
- Cancel: `CANCELLED` with canonical
  `{code: cancelled, source: control, message, retryable: false}`.
- Gone-agent runs: closed agent → `409 session_not_runnable`;
  never-existent id → `404 not_found` (contract: 409 covers closed).
- `reaper.stranded_running`: a session record fabricated `running` +
  stale past `--max-seconds` + grace on a live sandbox was finalized
  `lost` by the real `reap()` and its slot freed (was permanently
  stranded before the fix below).
- Cleanup: every gate-created agent terminal, `running=0/0`, zero
  leftover gate-owned sandboxes, all minted keys revoked.
- Leak scan: 62 captured responses + RC app log tail — 0 hits against
  the real credential watch-values and secret-shape patterns.

## PRODUCT_FAILs found and fixed on this branch

### 1. Dead-sandbox follow-up returned bare 500

`post_message` rebuilt a `SandboxHandle` from the stored
`sandbox_id` without a liveness check; `write_file`/`exec` on the dead
sandbox raised, `_rollback_turn` correctly finalized the record
`lost`, but the raw exception escaped `create_run` (only `KeyError`/
`SessionConflict` mapped) — an off-contract `500` where the contract
allows only `201/401/404/409`.

Fix: `control/service.py` — after dispatch failure, when rollback
left the record terminal, `post_message` raises
`SessionConflict("session_not_runnable")` (canonical `409`). The
same normalization covers the internal `/api/sessions/{id}/messages`
route. Regression:
`test_post_message_rolls_back_to_lost_when_sandbox_gone` tightened to
assert `SessionConflict` + `session_not_runnable` + `409`.

### 2. Stranded `running` record leaked its account slot forever

Observed live: a turn dispatched just before a control-plane cutover
lost its in-process watcher when the old container drained; the
record stayed `running` on a still-live sandbox indefinitely, holding
the account's only slot — the reaper covered `creating`/`idle`/dead/
terminal-orphan cases but never a `running` record on a live sandbox.

Fix: `control/reaper.py` — a `running` record stale beyond the
runner's own hard bound (`--max-seconds` = `turn_max_seconds`,
default 900s) plus `RUN_GRACE_S` (300s) margin is provably
watcher-less: mark `lost` first (frees the slot even if terminate
fails — retried by the `terminal_cleanup` pass), terminate the
sandbox, emit `lost` so the `/v1` lease-release hook fires. Wired
through `control/modal_app.py` as `plane.turn_max_seconds +
RUN_GRACE_S`. Regression coverage:
`test_stranded_running_turn_is_finalized_and_freed`,
`test_fresh_running_turn_is_left_alone`. End-to-end: the gate
fabricates the stranded record on a live RC sandbox and drives the
real `reap()` scoped to that session — `status=lost`,
`actions=['lost']`.

## Sibling-interference hardening (test-only)

Three release gates share the RC deployment concurrently. The gate
tolerates them without masking product failures:

- dedicated `sbx_` key (own per-key cap; revoked at teardown);
- `ensure_fleet()` upserts both grok records to `active`/1-slot before
  contended sections (sibling deploys reseed `grok-1` to 4 slots);
- foreign-session drain wait + retry-tolerant distribution loop;
- transient-tolerant `GET /v1/accounts` reads across cutover windows;
- build re-assertion deploy immediately before the
  code-version-sensitive stale-normalization check;
- cleanup asserts every gate-created agent reaches terminal state and
  the leftover scan is scoped to sandboxes owned by this gate's keys —
  sibling-held slots are not this gate's to judge.

## Local validation

- `make lint` — clean.
- `make test` — 1359 passed, 1 skipped (no cloud credentials).

## Artifacts

Per-run artifacts are ignored, not committed:
`tests/e2e_modal/artifacts/grok_fleet_gate.json`,
`grok_gate_grok-1.json`, `grok_gate_grok-2.json` (all `PASS`).

## Limitations

- A turn dispatched into a container that is then drained by *any*
  redeploy still strands until the reaper's `run_grace` window
  (20 min) — the fix bounds the leak; it does not migrate the watcher.
- The stranded run's open ledger record is not finalized by the reaper
  (slot release is the covered behavior; ledger truth for a
  watcher-less run stays `RUNNING` until the record closes).
- The RC app is shared: whichever gate deploys last determines the
  serving build. This gate re-asserts its own build before
  version-sensitive checks, but a sibling deploy landing mid-check can
  still flake a run — the artifact records it honestly.
