# Issue #199 progress and handoff

Plan: [`docs/architecture/issue-199-implementation-plan.md`](docs/architecture/issue-199-implementation-plan.md).
Nothing here has been merged or deployed.

| PR | Branch | State | Link |
| --- | --- | --- | --- |
| 1 Unified VM | `feat/issue-199-unified-vm-20261010` | In review | _pending_ |
| 2 Machine Slots + device login | `feat/issue-199-machine-slots` | Not started | |
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

## Pending

Everything in PRs 2-5. See the plan for scope and gates.

## Notes for the next session

- Live-test credentials stay on the build host; nothing secret is in this repository.
- Existing authorized test Volumes A1/A2/B1 live in the nonproduction Modal workspace and must
  only be mounted, never read or copied.
