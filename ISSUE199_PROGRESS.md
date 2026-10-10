# Issue #199 progress and handoff

Plan: [`docs/architecture/issue-199-implementation-plan.md`](docs/architecture/issue-199-implementation-plan.md).
Nothing here has been merged or deployed.

| PR | Branch | State | Link |
| --- | --- | --- | --- |
| 1 Unified VM | `feat/issue-199-unified-vm-20261010` | In review | https://github.com/soren-labs/sbx-agent/pull/200 |
| 2 Machine Slots + device login | `feat/issue-199-machine-slots` (stacked on PR 1) | Draft | https://github.com/soren-labs/sbx-agent/pull/201 |
| 3 Slot execution, catalog, effort | `feat/issue-199-slot-execution` (stacked on PR 2) | Draft | _pending_ |
| 4 SDK/CLI + Console | `feat/issue-199-cli-console` | Not started | |
| 5 Staging E2E + docs | `feat/issue-199-e2e-docs` | Not started | |

## PR 1 - done

- Modal SDK pinned `>=1.6.1`; the single `Sandbox.create` call uses `runtime="vm"`, no Secrets, no OIDC token.
- Shared image recipe split from its build; `ModalExecutor.prewarm` runs from the new
  `connection.provision` Job after a Modal Connection validates.
- `JobContext.keepalive()` renews the claim during lease provisioning (#198);
  `executor.bound.provisioning` reports per-stage milliseconds.
- Live checks: `tests/e2e_modal/smoke_executor.py` (VM kernel proof + DeepSeek Turn) and
  `tests/e2e_modal/mvp_acceptance.py`.

## PR 2 - done (backend)

- Migration `0006_machine_slots.sql`: `machine_slots`, `slot_login_attempts`, Job target family.
- `runtime/subscriptions/setup.py`: provider-neutral Setup VM supervisor (stdin always closed).
- `control/integrations/subscriptions/`: adapter contract and the Codex adapter.
- `control/application/slots.py`: Slot commands, login state machine, cleanup, delete/logout.
- `ModalExecutor.setup_start/setup_observe/volume_delete`; one shared `Sandbox.create` path.
- Routes under `/api/machine-slots`; spec `docs/specs/unified/machine-slots.md`.
- Live check `tests/e2e_modal/slots_acceptance.py` (`SBX_SLOT_LOGIN=cancel|approve`).

Open item: the brand-new device login to Ready needs a person to approve the code. The
`approve` mode of the live check waits for that; everything else is verified on real VMs.

## PR 3 - done (backend)

- Migration `0007_slot_sessions.sql`: `sessions.machine_slot_id`, `sessions.harness_effort`,
  `executor_leases.machine_slot_id`.
- Catalog: supervisor asks `codex app-server` `model/list`; adapter normalizes; stored with source
  and timestamp in `machine_slots.capabilities.catalog`.
- Sessions: `inference.mode=subscription`, server-side model/effort validation, no API fallback.
- Execution: holder taken with the lease, Volume mounted into the one Worker, `sync` after each
  Turn and before termination, Slot freed from durable lease state.
- Codex Harness subscription mode (`CODEX_HOME` on the Volume, `-c model_reasoning_effort`).
- Custom API: reasoning probe in `inference_api` validation; thinking-off for OpenCode only.
- Live check `tests/e2e_modal/slots_parallel_acceptance.py`: A1+A2, A1+B1, A1+A2+B1.

Measured facts worth keeping:

- The provider accepts an effort a model does not list; SBX's server-side check is what enforces
  the catalog. The effort does change provider behaviour (reasoning tokens low 144 vs ultra 258).
- DeepSeek honours `none` (thinking off) on all three protocols; graded values are accepted but
  show no consistent effect, so only on/off is offered.
- Codex CLI 0.162.0 does not forward `model_reasoning_effort` to a custom Responses endpoint.

## Pending

- PR 4: SDK/CLI `sbx slots`, Console "My Cloud Machines", Slot/model/effort pickers in New
  Session, and the recorded browser login (needs a person to approve one device code).
- PR 5: staging E2E through the deployed Console, docs-site pages, security review.
- Not done in PR 3: thinking control for the Claude Code, Grok and Command Code Harnesses with a
  custom API (not verified, so not offered).
