# Modal VM + durable profile Volume MVP

**PASS — real cloud, 2026-10-10.** SDK 1.6.1 launched fresh VM-backed Sandboxes,
preserved a synthetic profile across VM destruction, and isolated two synthetic
slots with separate Volumes while both VMs were running. This is an experiment,
not a shipping SBX feature or validation of two user accounts.

Branch: `experiment/codex-modal-vm-mvp-20261010`; original base: `b96a7c9`.
Scope follows the task prompt associated with issue #199. Production executor,
dependencies, APIs, database and Console are unchanged. Existing
`control/executors/modal.py` synchronously calls `image.build(app)` and does not
select a runtime. `pyproject.toml` permits `modal>=1.5.5,<2`; its lower bound was
not tested for VM support. The workstation CLI's isolated pipx SDK is **1.6.1**.

## Invocation and reproduction

The installed 1.6.1 `inspect.signature(modal.Sandbox.create)` accepts
`runtime: Optional[Literal['gvisor', 'vm']]`. The supported invocation is
`modal.Sandbox.create(..., runtime="vm")`, confirmed by the
[current Sandbox guide](https://modal.com/docs/guide/sandboxes) and
[Python reference](https://modal.com/docs/reference/modal.Sandbox).
An older beta guide uses `experimental_options={"vm_runtime": True}`; this probe
uses the explicit supported runtime argument instead.

Exact confirmation command on this workstation:

```bash
MODAL_PROFILE=sbx-ci /home/ubuntu/.local/share/pipx/venvs/modal/bin/python \
  experiments/modal_vm_mvp/probe.py --run-cloud \
  --label 20261010-codex-01 --output experiments/modal_vm_mvp/results.json
```

Portable isolated SDK setup, without changing project dependencies:

```bash
uv venv --python 3.12 /tmp/sbx-modal-vm-mvp-sdk
uv pip install --python /tmp/sbx-modal-vm-mvp-sdk/bin/python 'modal==1.6.1'
MODAL_PROFILE=sbx-ci /tmp/sbx-modal-vm-mvp-sdk/bin/python \
  experiments/modal_vm_mvp/probe.py --run-cloud \
  --output /tmp/sbx-modal-vm-mvp-results.json
```

Requires an existing authorized nonproduction Modal profile. Choose a fresh
label (default: UTC timestamp); do not run the same label concurrently. The
script creates only names beginning `sbx-exp-vm-mvp-`, injects no Secrets or OIDC
token, blocks outbound Sandbox networking, and adds no local files to its image.
It never accesses local Codex state or forwards host environment variables.
Only the local SDK uses the existing Modal profile for control-plane RPCs.

## Evidence

App: `sbx-exp-vm-mvp-20261010-codex-01`.
Private workspace Volumes v2: same prefix with `-slot-a` and `-slot-b`.
These are not public shares or separate Modal workspaces; isolation here means
each VM receives only its selected Volume, not an authorization audit.
Image: `im-GYYHaMJMKETlbgCvh62lOr`, Debian slim / Python 3.12; no Codex installed.
Each VM requested 1 physical CPU core, 1024 MiB RAM, and a 180-second timeout.

Final measured run (07:16:46–07:16:55 UTC; full output in [results.json](results.json)):

| Stage | Sandbox ID | Create call | Create through first exec |
| --- | --- | ---: | ---: |
| First, unbuilt image recipe | `sb-01M4JAQRW9AH04K20Y7ZJSRK7M` | 0.365s | 1.511s |
| Fresh replacement, built image handle | `sb-01M4JAQW2CBQNSKM6NJHF2XYVN` | 0.094s | 1.180s |
| Concurrent slot B | `sb-01M4JAQXMH7QQMJRDWM0E6XNWN` | 0.186s | 1.051s |

Shell capability output:

```text
Linux cloud-hypervisor 7.2.9 #1 SMP PREEMPT Sun Oct  4 19:32:00 UTC 2026 x86_64 GNU/Linux
pid1=runch-agent
pid_namespace=pid:[4026531836]
cgroup_filesystem=cgroup2fs
nested_pid=1
sh
tmpfs_mount=tmpfs
```

`unshare --mount --pid --fork --mount-proc` and a tmpfs mount/unmount both
succeeded. No Docker daemon, FUSE workload or nested virtualization was tested.
The first and replacement kernels had distinct boot IDs
`2f5e2e5b-99ce-4406-a22f-b004d32f1e27` and
`d531d1db-81a5-4b3e-951b-f58fda607df1`. A file under `/tmp` disappeared,
while `/mvp-profile/.codex/mvp-marker` retained exactly
`synthetic-profile-fad01092313b41ea82ae816c21b510e7`.
`HOME=/mvp-profile` and `CODEX_HOME=/mvp-profile/.codex` model the profile path;
there is no auth file, token or personal profile.

Writes were explicitly committed using `sync /mvp-profile` (exit 0), followed
by `terminate(wait=True)` before the replacement boot. This is the documented
[Volume v2 Sandbox commit mechanism](https://modal.com/docs/guide/sandbox-files).
Both concurrent VMs were polled running; A saw only `slot-a-only`, B saw only
`slot-b-only`, and B could not see A's synthetic profile. Every shell assertion
uses `set -eu`; all commands exited 0. Distinct slot mounts demonstrate storage
isolation, not two authenticated subscriptions.

## Latency, cost and cleanup

Passing an **unbuilt** `Image.debian_slim(...)` recipe to VM creation worked:
there is no requirement for callers to supply a prebuilt image handle.
The later explicit `image.build(app)` took **0.093s**, a cache lookup after
first use. These are fresh VM starts against a possibly cached base image,
not a cache-free image build benchmark or SBX runtime readiness measurements.
The earlier exploratory run measured 1.680s / 1.313s / 1.400s and also terminated
all three VMs. No large SBX image was built; Opus should measure its build
separately and move expensive builds out of allocation if appropriate.

All **six** VMs across both runs terminated (expected forced termination code
137). The final app-scoped SDK sweep recorded `running_ids: []`. SIGINT/SIGTERM
and exceptions enter cleanup; lost create handles are covered by an app-scoped
sweep, with the 180-second timeout as a fallback if the client dies.

Slot B was deleted after the run using:

```bash
modal volume delete sbx-exp-vm-mvp-20261010-codex-01-slot-b --profile sbx-ci --yes
```

Only slot A's **58 bytes** of synthetic files are retained for inspection/reruns;
post-run readback and slot B's absence were verified in `results.json`. New
reruns retain their test Volumes; delete B using the new label after inspecting
the results, and optionally delete A too. No
deployed Functions exist; the labeled app and cached base image may remain as
metadata/cache. To remove the retained experimental data:

```bash
modal volume delete sbx-exp-vm-mvp-20261010-codex-01-slot-a --profile sbx-ci --yes
```

Nominal request-based compute was well below $0.01 for the short runs; actual
invoice usage was not queried. At the published Sandbox rates of
$0.00003942/core/s + $0.00000667/GiB/s, six full 180-second lifetimes would be
about $0.05 at these requested resources, excluding builds/storage/overheads.
Volume pricing lists $0.09/GiB/month with 1 TiB/month included. These are estimates,
not verified charges; see [Modal pricing](https://modal.com/pricing).

Validation: real-cloud probe PASS, `make lint` PASS, and repository layer boundary
tests PASS. No product backend/frontend/specification changes require additional
product suites or public documentation-site checks.

## Handoff to Opus 5.5

1. Deliberately integrate `runtime="vm"` with a tested SDK minimum and the SBX
   runtime image. Measure real image build and daemon readiness separately.
2. Design durable account-to-private-Volume ownership and scoped mounts, with
   credential access controls, revocation/deletion, and profile permissions.
   This experiment is not an approved production credential storage design.
3. Make profile writes/refresh durable with explicit v2 commits and a tested
   restart path. Fence profile writers until same-account concurrency semantics
   are established; test cancellation/recovery and stale mounted views.
4. **Next manual gate:** create a separate guarded official Codex setup inside
   Modal, where the user completes device login themselves. Do not import this
   workstation's Codex credentials or print profile contents. No login command
   was started, and no authorization was attempted in this experiment.
5. Validate subscription inference after a fresh VM boot, OAuth refresh and
   expiration, then same-account concurrency and finally two independently
   authenticated accounts. None of these behaviors was demonstrated here.

Implement account scheduling/UI/database contracts in the later feature work,
not by promoting this probe into the production executor.
