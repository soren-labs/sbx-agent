# Issue #199 progress and handoff

Plan: [`docs/architecture/issue-199-implementation-plan.md`](docs/architecture/issue-199-implementation-plan.md).
Nothing here has been merged or deployed.

| PR | Branch | State | Link |
| --- | --- | --- | --- |
| 1 Unified VM | `feat/issue-199-unified-vm-20261010` | In review | https://github.com/soren-labs/sbx-agent/pull/200 |
| 2 Machine Slots + device login | `feat/issue-199-machine-slots` (stacked on PR 1) | Draft (stacked) | https://github.com/soren-labs/sbx-agent/pull/201 |
| 3 Slot execution, catalog, effort | `feat/issue-199-slot-execution` (stacked on PR 2) | Draft (stacked) | https://github.com/soren-labs/sbx-agent/pull/202 |
| 4 SDK/CLI + Console | `feat/issue-199-cli-console` (stacked on PR 3) | Draft (stacked) | https://github.com/soren-labs/sbx-agent/pull/203 |
| 5 Staging E2E + docs | `feat/issue-199-e2e-docs` (stacked on PR 4) | Draft (stacked) | https://github.com/soren-labs/sbx-agent/pull/204 |

Merge order is 1 → 5; each PR's base is the previous branch.

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

## PR 4 - done

- SDK `client.slots`, CLI `sbx slots ...` and `sbx sessions create --slot --model --effort`.
- Console `/machines` ("My Cloud Machines") and the Runs on / model / effort pickers in New Session.
- A person approved one real device login from the Console ("Codex 19" on staging); the row turned
  Ready by itself and ran real tasks. Evidence: `docs/reports/issue-199/pr-04/`.

## PR 5 - done

- docs-site: guide `guides/cloud-machines`, CLI and SDK reference, providers, connections, console,
  security, troubleshooting, changelog.
- Recorded Console E2E on staging, 9/9 gates, and the requirement map:
  `docs/reports/issue-199/pr-05/README.md`.

## Staging used for the evidence (outside the repository)

`~/sbx-issue199/stage/up.sh` restarts PostgreSQL (:54399), the control plane (:8810) and the Console
(:5184); Playwright scripts are in `~/sbx-issue199/pw/`. The staging workspace still holds the
machine "Codex 19" with the login approved on 2026-10-10 (Volume in the nonproduction Modal
workspace). Sign it out or delete it from the Console when it is no longer wanted.

## Open

- Review and merge in order; nothing is merged or deployed.
- Sign out and log in again are covered through the API on real VMs but have no browser recording.
- Thinking control for the Claude Code, Grok and Command Code Harnesses with a custom API is not
  verified, so not offered.
