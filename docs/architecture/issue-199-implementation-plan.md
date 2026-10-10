# Issue #199 implementation plan

Unified Modal Linux VM, Codex Machine Slots, truthful model/effort controls, SDK/CLI and Console.
The issue's "Active implementation spec (2026-10-10)" is the requirement source; this file fixes
the PR order, the design decisions that span PRs and the known risks. Status lives in
[`ISSUE199_PROGRESS.md`](../../ISSUE199_PROGRESS.md).

## Vocabulary

| Term | Meaning |
| --- | --- |
| Machine Slot | One independent official subscription login: a durable row plus one private Modal Volume v2 in the owner's Modal workspace. Not a running VM. |
| Setup VM | Short-lived `runtime="vm"` sandbox that runs the provider's official device login against the Slot Volume, then is destroyed. |
| VM Worker | The executor lease's sandbox for a Session. A Slot is leased to at most one Worker at a time. |
| Inference mode | `custom_api` (an `inference_api` Connection, e.g. DeepSeek) or `subscription` (a Machine Slot). |

## PR sequence

Each PR is stacked on the previous branch until its base merges. No PR merges or deploys itself.

| PR | Branch | Scope | Gate evidence |
| --- | --- | --- | --- |
| 1 | `feat/issue-199-unified-vm-20261010` | Modal SDK `>=1.6.1`; every sandbox `runtime="vm"`; one shared image recipe prewarmed on Connection verification; claim keepalive during provisioning and per-stage timings (#198). | Real VM boot, DeepSeek BYOK Turn, full API to PR acceptance, cold/cached image timings. |
| 2 | `feat/issue-199-machine-slots` | Migration `0006`: `machine_slots`, `slot_login_attempts`. Provider-neutral `SubscriptionAdapter` port with the Codex adapter. Setup VM device login, verification with a real CLI call, Volume sync, reauth/revoke/delete, crash-safe reaper. `/api` routes and OpenAPI. | Real device login to Ready with the Setup VM destroyed; existing A1/A2/B1 Volumes adopted without reading them. |
| 3 | `feat/issue-199-slot-execution` | Session `inference_mode` + `machine_slot_id`; transactional one-Worker-per-Slot lease; Slot Volume mounted only into its Worker; Volume sync on release; per-Slot model catalog from the authenticated CLI; reasoning effort validated server-side and applied natively; DeepSeek thinking/effort mapping. | A1+A2, A1+B1 and A1+A2+B1 overlapping real Turns with fresh boot IDs and reclaimed VMs. |
| 4 | `feat/issue-199-cli-console` | SDK and `sbx slots ...` commands; Console "My Cloud Machines" with grouped, collapsible, filterable compact rows; live device-login flow; Slot/model/effort pickers in New Session. | Browser recording of the real login to Ready to Turn flow; 15-20 Slot screenshots at 320 px and desktop, light and dark. |
| 5 | `feat/issue-199-e2e-docs` | Staging E2E scripts, tenant-isolation and redaction tests, README/docs-site/architecture/OpenAPI/CLI docs, migration and operations notes. | Full staging run and docs build. |

## Cross-PR design decisions

1. **One runtime.** `control/executors/modal.py` has a single `Sandbox.create` call site with
   `runtime="vm"`. Setup VMs and Workers both go through the executor so later PRs cannot fork it.
2. **Image.** Modal images are per workspace, so the shared image is built in each owner's
   workspace, keyed by recipe digest, and prewarmed by `connection.provision`. First allocation
   after a recipe change still works: it builds under the claim keepalive.
3. **Slot storage.** The control plane stores only the Volume name, state and safe metadata.
   Credentials are written by the official CLI inside the VM and never cross the tunnel, the API,
   the database, logs or events. The control plane never lists or reads the profile directory.
4. **Slot exclusivity.** A Slot holds `leased_by_lease_id` + `lease_generation`, taken in the same
   transaction that creates the executor lease and cleared by `executor.release` after the Volume
   sync is confirmed. A Setup VM takes the same lock, so login and execution never share a Volume.
5. **No fallback.** A Session in `subscription` mode never receives an inference API key, and a
   `custom_api` Session never mounts a Slot Volume.
6. **Catalog truth.** Model IDs and reasoning efforts are taken only from the authenticated CLI
   for that Slot and cached with source and timestamp. With no trustworthy catalog the picker is
   disabled; nothing is hardcoded in the Console.
7. **Noninteractive CLI calls** always close stdin (`subprocess.DEVNULL`).

## Risks

| Risk | Handling |
| --- | --- |
| VM runtime availability or behaviour differs from gVisor | PR 1 runs the full existing acceptance on VMs before anything is built on top. |
| Cold image build exceeds one claim lease (#198) | Prewarm on Connection verification; bounded keepalive; idempotent adoption by operation ID. |
| Official CLI offers no machine-readable model catalog | Investigate first in PR 3; if none exists, document the blocker and keep the picker disabled rather than invent IDs. |
| Token refresh written by a Worker is lost | Explicit `sync` of the Volume before termination; release is not confirmed until it succeeds. |
| Device login needs a human | Only the auth-dependent step stops; the real URL and code are surfaced to the user. |
| Subscription limits | Slots add isolated logins, not quota; documented, never bypassed. |
