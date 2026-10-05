# Release 0.1 — real Modal Antigravity fleet gate

Reviewer: Devin (gate author, SOR-68 scope)
Reviewed HEAD: `5ea5fc2a0dfef7e2ad3f2efa3d141e77a08a7d8a`
(`release/0.1-gate-agy` at the exact successful RC deploy commit —
"Release 0.1 RC deploy: isolated RC namespace + remote PYTHONPATH fix")
Result: **PASS — 50/50 checks green, zero failures.** No product code
changed; driver + this note only.

## Environment under test

| Item | Value |
| --- | --- |
| Modal app | `sbx-control-release01-rc` (isolated RC namespace) |
| Endpoint | `https://sorenlab2026--sbx-control-release01-rc-fastapi-app.modal.run` |
| Modal profile | `sorenlab2026` |
| Bootstrap key fingerprint | `sha256:2ba85f236e42` (key material never printed) |
| Fleet | `antigravity-1..4`, seeded `slots=1` via `SBX_ANTIGRAVITY_ACCOUNTS` |
| RC Dicts | `sbx-rc-{sessions,runs,accounts,workflows,artifacts,workspaces}` |
| Per-account Secrets | `sbx-rc-acct-antigravity-{1..4}` |
| Credential fingerprints | `01e57d639ea27797` / `24ff56f9c3345c49` / `2ef2842bdadf503f` / `458e58181a16004d` |
| Gate driver | `tests/e2e_modal/agy_fleet_gate.py` |
| Artifact | `tests/e2e_modal/artifacts/agy_fleet_gate.json` |
| Passing run | 2026-09-16 13:12–13:26 CST (~14 min), `verdict=PASS`, `failed=[]` |

All tasks were trivial (`"Reply with exactly this line and nothing else:
AGY_OK"` and marker-recall variants). No production resources or accounts
were touched.

## What the gate proved (all real, all green)

### Fleet provisioning & `account_id=auto` distribution

- RC app redeployed with the 4×1-slot Antigravity fleet seed; all four
  accounts reported `active`, `running=0`, `max_concurrent=1` after the
  reseed landed (`fleet.all_active`, `fleet.slots_1_each`).
- Four `account_id="auto"` creates (split across bootstrap + minted
  agents-scope keys to respect the per-owner cap of 2) resolved to four
  **distinct** accounts: `antigravity-{1,2,3,4}`, one running session each
  (`dist.four_distinct_accounts`, `dist.running_one_each`,
  `dist.assignments`).
- All four run-1 turns executed the real Antigravity CLI end-to-end and
  returned `AGY_OK` (`dist.run1_all_finished` — `FINISHED`, marker present
  per account).

### Slot / full behavior

- Named create on an occupied account → `409 account_busy`.
- Missing account → `409 account_unavailable`.
- Wrong-provider account (`codex-1` under `provider=antigravity`) →
  `409 account_unavailable`.
- Full pool auto create → `429 provider_exhausted` with `retry_after=60`
  (`slots.auto_exhausted_429`, `slots.exhausted_retry_after`).

### Two-turn resume

- Turn 2 finished on the same agent; turn 3 recalled the marker phrase from
  turn 1 without file/command access (`resume.run3_recalls_marker`).
- All turns share one Antigravity thread
  (`resume.same_thread_turns`, `threads=1`);
  `native_session_id` recorded on the session.

### Stale-conversation semantics

- `session.json` was corrupted in the live sandbox to a bogus
  `native_session_id` (`00000000-0000-4000-8000-000000000000`).
- The next run was accepted, then failed `ERROR` with structured
  `code=runtime_error`; the requested stale id was preserved verbatim
  (`stale.keeps_requested_id`) and the account stayed `active`
  (runtime error ≠ provider-health failure).

### Unavailable / cooldown / auth-invalid / failover

- `cooling` (target `antigravity-1`, cooldown_until +120s): named create →
  `409 account_unavailable`; auto create skipped it to `antigravity-2`;
  after expiry the account lazily recovered to `active` and accepted a
  named create.
- `disabled` (target `antigravity-4`): named `409 account_unavailable`;
  auto skipped to `antigravity-3`; re-enable restored `active`.
- **Real auth-invalid** (target `antigravity-1`): the per-account Modal
  Secret was swapped for a same-shape blob with corrupted credential
  leaf values only; the real turn ended `ERROR code=auth_invalid
  (source=provider)`; the reporter marked the account `invalid`; auto
  create failed over to `antigravity-4`. The original credential blob was
  then restored to the Secret, the account re-activated, and a real
  restored-credential turn finished `FINISHED` — valid credentials were
  never destroyed.

### Restart running-count safety

- Two idle agents held `antigravity-1`/`antigravity-2` slots
  (`restart.pre_counts` `{1,1,0,0}`); the control app was redeployed;
  post-restart counts were unchanged (`restart.counts_survive`).
- A post-restart `account_id=auto` create on a freshly minted key landed on
  a genuinely free account (`antigravity-3` — no oversell of held slots)
  and executed a real turn to `FINISHED`
  (`restart.auto_uses_free_slot`, `restart.post_turn_finished`).

### Leak scan

- 226,443 bytes captured across HTTP responses, sandbox `session.json` /
  `turns/*.json` / `events*.jsonl`, the Modal app-log tail, and the
  serialized artifact — zero hits against every watched secret substring
  and the generic leak patterns (`leak.no_secret_material`, `hits=[]`).

### Cleanup

- All gate-created agents deleted; minted API keys revoked; all four fleet
  accounts restored to `active`; the gate-owned Modal sandbox sweep found
  `leftover=[]` (`cleanup.accounts_active`, `cleanup.no_gate_sandboxes`).

## Run history / contention note

This deployment is shared with sibling Release-0.1 gates (grok, workflow,
core) running in parallel on the same host. Earlier gate attempts (run 1–4)
recorded FAILs that were diagnosed as **infrastructure contention, not
product failures**:

- a create landing on a draining pre-deploy container
  (`modal.exception.ClientClosed` mid-provision) — fixed by waiting for the
  reseeded slot caps as the readiness signal plus a drain settle;
- sibling-held Antigravity slots and global-cap saturation returning
  `429 provider_exhausted` / `429 concurrency_limit` on expected-success
  paths — fixed by bounded retries on capacity codes only, dynamic
  free-account targeting for the failover/restart scenarios, and a shared
  burst deadline for the 4-way distribution;
- a full-credential-leaf bogus blob occasionally hanging the CLI until the
  runner's 900s soft timeout — fixed by corrupting only token-value leaves
  (`access_token` / `refresh_token` / `id_token`), which now produces a
  deterministic fast `auth_invalid`;
- a cleanup check that counted sibling-held `running` slots as a gate leak —
  replaced by an ownership-scoped sandbox sweep plus an all-active account
  assertion.

No `PRODUCT_FAIL` was ever confirmed; no product code was modified. The
final green run executed while no sibling gate was active.

## Local checks

- `uv run ruff check tests/e2e_modal/agy_fleet_gate.py` — clean
- `uv run ruff format --check tests/e2e_modal/agy_fleet_gate.py` — clean
- Artifact re-scanned post-hoc: no `sbx_` key material, `ya29.*` tokens,
  unredacted `access_token`/`refresh_token`, or private-key blocks.

## Sanitization

This document and the artifact contain only account ids, agent-id
prefixes, error codes, statuses, short SHA-256 fingerprints, and the
public sanitized RC URL. The bootstrap key, minted API-key tokens, and
per-account credential blobs were never printed, logged, or recorded —
the driver redacts minted-key responses before capture and watches for
every known secret substring.
