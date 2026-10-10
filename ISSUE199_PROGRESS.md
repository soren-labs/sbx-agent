# Issue #199 progress and handoff

Plan: [`docs/architecture/issue-199-implementation-plan.md`](docs/architecture/issue-199-implementation-plan.md).
Nothing here has been merged or deployed.

| PR | Branch | State | Link |
| --- | --- | --- | --- |
| 1 Unified VM | `feat/issue-199-unified-vm-20261010` | In review | https://github.com/soren-labs/sbx-agent/pull/200 |
| 2 Machine Slots + device login | `feat/issue-199-machine-slots` (stacked on PR 1) | In review | _pending_ |
| 3 Slot execution, catalog, effort | `feat/issue-199-slot-execution` | Not started | |
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

## Pending

- PR 3: Session `inference_mode`/`machine_slot_id`, Worker holder lease with the Slot Volume
  mounted, Volume sync on release, per-Slot model catalog from the authenticated CLI, reasoning
  effort end to end, DeepSeek thinking mapping, 2-way and 3-way live overlap.
- PR 4: SDK/CLI `sbx slots`, Console "My Cloud Machines", pickers in New Session.
- PR 5: staging E2E, docs-site, security review.

## Notes for the next session

- Live-test credentials stay on the build host; nothing secret is in this repository.
- Existing authorized test Volumes A1/A2/B1 live in the nonproduction Modal workspace and must
  only be mounted, never read or copied.
