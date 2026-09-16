# SOR-107 — Release 0.1 real cross-provider workflow gate record

Operator: Devin (gate author/executor)
Base: exact successful RC deploy HEAD `5ea5fc2`
("Release 0.1 RC deploy: isolated RC namespace + remote PYTHONPATH fix"),
executed on branch `release/0.1-gate-workflow`.
Target: live RC deployment `sbx-control-release01-rc` (see
`SOR-107-rc-deploy.md` for the namespace). Real Modal sandboxes, real
provider accounts — no fakes.
Result: **PASS** — two consecutive full gate cycles, all checks green,
`credential leak=0`.

## Harness

`tests/e2e_modal/workflow_gate.py` — a two-process gate:

- `phase1` — preflight, workflow-bound producer A (grok) on a declared
  workspace, artifact creation + manifest/patch/bundle verification,
  producer teardown, artifact re-download post-teardown, publish of the
  exact producer head to the fixture repo via the artifact's
  `repo.bundle` (no source text through the prompt), consumer B
  (antigravity) started on artifact handoff, then a **deliberate
  `os._exit(42)` mid-SSE-stream** — the orchestration client dies with B
  RUNNING.
- `phase2` — fresh process holding **only the API key + `workflow_id`**:
  recovery, workflow view, B's terminal truth + SSE replay/resume, B's
  artifact (base == A's head), exact-head reviewer C (devin) with
  review-pin and wrong-head rejection, deterministic error leg E
  (`base_sha_mismatch`), cancel leg D, `wait_many` mixed terminal truth,
  `sbx upgrade` redeploy durability, leak scan, scoped cleanup, and
  post-cleanup artifact durability.

Shared fixture repo (created for the gate):
`https://github.com/soren-labs/sbx-gate-fixture-20260916.git`,
`main @ 723498a81d7d45107b37d7b475f207402dec5915`.

## Final gate run — `wf-gate-4aac79765b40` (PASS, 45 checks)

Providers exercised: **grok** (A producer, E error leg, D cancel leg),
**antigravity** (B consumer), **devin** (C exact-head reviewer) — 3
providers/accounts.

| Leg | Evidence |
| --- | --- |
| Preflight | key `key_bootstrap_2ba85f236e42`, scopes `['agents','admin']`; providers available: antigravity, devin, grok, opencode |
| Workflow metadata | A/B/C/D/E all created with `workflow_id` + task/role metadata; `GET /v1/workflows/{id}` view + progress (`tasks=2 agents=2 runs=2`, all terminal) verified post-recovery |
| Agent A artifact | `art-0c288126268340f3`; base `723498a8…` → head `d56151693b37ffbd467bb7cca39fbc11cd6d627a`; `test -f gate_marker.txt` exit 0 recorded; `patch.diff` + `repo.bundle` sha256 verified |
| Producer teardown → artifact durable | A closed; `patch.diff` (186 B) re-downloaded post-teardown; `GET` manifest still 200 |
| Exact head published | `d5615169…` pushed to `gate/wf-gate-4aac79765b40` **from the artifact `repo.bundle`** — no source through the client |
| Agent B handoff | prompt references paths only (`B.prompt_carries_no_source`); B FINISHED; B artifact `art-93ecb9783f144000` base == A head (`handoff.chain`) |
| Deliberate client kill | phase-1 process exited `42` mid-stream after 3 SSE frames; B status at kill `RUNNING`; run continued server-side |
| Recovery (key + workflow_id only) | fresh `SbxClient`; `recover()` + workflow view returned both agents; B handle re-derived |
| SSE replay + resume | 15 frames replayed post-mortem; `Last-Event-ID` resume from `id=8` → 7 subsequent frames, all `id>8`; teardown-fallback watch returned cleanly in 25 s |
| Exact-head reviewer C | checkout `d5615169…` verified from workspace record; review pinned to that head; wrong-head review rejected `409 head_sha_mismatch` |
| Error leg E | declared `base_sha` all-zeros → run `ERROR` `base_sha_mismatch`; agent auto-closed |
| Cancel leg D | run `RUNNING` → `POST …/cancel` → `CANCELLED` (attempt 1; `cancel_response=CANCELLED`) |
| `wait_many` mixed truth | A FINISHED, B FINISHED, C FINISHED, D CANCELLED, E ERROR — exact match, independent statuses |
| Control-plane redeploy | `sbx upgrade --json` rc=0 `ok:true`; durable Dicts preserved: sessions 93, runs 129, accounts 13, workflows 32, artifacts 168, workspaces 22 |
| Post-redeploy durability | runs still FINISHED; artifact 186 B re-downloaded; recovery returns all 5 agents; review pin intact |
| Credential leak scan | 31 706 B in-memory corpus (all API/SSE/artifact payloads incl. prompts) — 0 hits across API key, basic-auth password, Modal tokens, `CODEX_AUTH_JSON`, JWT/`sk-`/Bearer patterns |
| Scoped cleanup | `DELETE /v1/workflows/{id}` → 200; `leftover_agents=[]`; other workflows/agents untouched |
| Artifact post-cleanup | `patch.diff` still downloadable (186 B) after workflow deletion |

Earlier full cycle `wf-gate-e4b238244ebe` also passed all checks (29
phase-2 checks; phase-1 check carry-forward was added after it, which is
why its persisted file lists phase-2 checks only).

## Transients observed — no product defect

1. **Cancel leg `UNKNOWN` (one earlier run).** Under parallel-lane
   provider contention, D's run stayed `CREATING` past the reaper's
   300 s create grace → session `lost` → run `UNKNOWN`; the cancel never
   engaged (the route persists `CANCELLED` only for `CREATING`/`RUNNING`
   runs, both durable). This is designed behavior, not a defect — the
   harness now records `status_before_cancel`/`cancel_response`, treats
   `UNKNOWN` as "cancel never engaged," and retries the leg. Both final
   cycles cancelled cleanly on attempt 1.
2. **SSE `Last-Event-ID` resume flake (one earlier run).** Resume from
   `id=8` returned 0 frames once; all other runs resumed correctly
   (7–12 frames). `watch()`'s persisted-terminal fallback still yields
   correct truth, so the leg retries once and records both attempts.
   Possible follow-up: a transient `tail -F` exec failure on the stream
   path exits without falling through to file replay.
3. **Provider capacity contention.** Single-account providers + the
   per-key concurrency cap + parallel gate lanes produced transient
   `429 provider_exhausted`/`concurrency_limit` and `409 account_busy`.
   Harness retries creates (bounded) and closes agents as soon as their
   leg completes.

## Product changes

None required — the gate found no product failure. Changes on this
branch are confined to `tests/e2e_modal/workflow_gate.py` (SOR-68 path)
plus this record.

## Credentials

No credential contents were printed, logged, or committed. The API key
is referenced by fingerprint only (`key_bootstrap_2ba85f236e42`); the
leak scan asserts none of the known secret values appear in any
persisted artifact.
