# SOR-84 independent review

Base `ec40b00` → integration HEAD `bfcaa55` (C1 + C2 + integration). Reviewed
against the SOR-84 Linear acceptance criteria: workflow metadata durability,
SSE id/resume, `wait`/`wait_many`/`watch`/`recover`, scoped cleanup, honest
usage, and SOR-82 regression surface.

## Blocking defects found and fixed in this review commit

1. **Stale workflow index could hide live agents from recovery/cleanup.**
   `WorkflowStore.list_workflow` served the index alone whenever it decoded,
   falling back to an agent-record scan only when the index was missing or
   corrupt. A crash between the agent-record write and the index write (or a
   lost update on a backend without read-modify-write, e.g. `modal.Dict`)
   left a valid-but-stale index that permanently hid the agent from
   `GET /v1/workflows/{id}`, `?workflow_id=` filtering and `DELETE` cleanup —
   contradicting the module's own invariant. `list_workflow` now merges the
   index over an authoritative agent-record scan, and index slots are
   suppressed for agents whose own record names a different workflow, so a
   lost index-removal cannot double-list a moved agent either.
   Regression tests: `test_stale_index_cannot_hide_an_agent`,
   `test_stale_index_slot_does_not_double_list_a_moved_agent`.

2. **`WorkflowAgent.latest_run` violated the `Run` contract.**
   The view omitted `agent_id` (a required `Run` field) and emitted
   `created_at`/`updated_at` as `null`/`""` for derived or corrupt records
   (`Run` requires non-null `date-time`). `_run_view`/`_derived_latest_run`
   now always populate `agent_id`, fall back to session timestamps, and carry
   `provider`/`account_id`/`model`; `usage` is included only when measured.
   Regression tests: `test_latest_run_keeps_run_contract_shape`,
   `test_derived_latest_run_keeps_run_contract_shape`.

## Verified, non-blocking observations

- `SbxClient.close_workflow` composes `recover` + per-agent `DELETE` instead
  of the new `DELETE /v1/workflows/{id}` route. Functionally correct and
  idempotent (the server returns the closed record on re-delete); the
  purpose-built route additionally tolerates agents already gone. Worth
  revisiting if the SDK should prefer the single-call form.
- `wait`/`wait_many` surface `httpx.TransportError` rather than retrying a
  poll — `watch` does absorb transport errors. Callers can re-call `wait`;
  noted as a possible follow-up, not gated by the issue.
- `POST /v1/agents/{id}/runs` metadata re-binds under the caller's key id.
  Cross-key runs are already unrestricted in the ambient `/v1` model (any
  key can get/delete any agent), so this weakens nothing existing; the
  workflow index stays strictly tighter. Broader per-key ownership
  enforcement is a separate design question.
- `docs/contracts/api-v1.yaml` was extended in-lane (workflow routes,
  `metadata`, `workflow_id` filter, nullable `usage`); consistent with the
  `x-canonical` route list and `test_contract_routes` (22 ops).
- Credential hygiene: workflow records carry no secret material; the
  leak-hygiene test asserts no token/secret markers in the store files.
- SOR-82 surface (idempotent async create, durable ledger, structured run
  errors) unaffected; `usage: None` propagates as `null` on `/v1` while
  `/api` keeps the zero-filled shape.

## Verification run

- `make lint` — clean.
- `make test` (`tests/unit` + `tests/integration`) — 762 passed, 1 skipped.
