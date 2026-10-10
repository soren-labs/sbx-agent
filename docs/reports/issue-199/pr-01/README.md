# PR 1 evidence: unified Modal VM runtime

Real nonproduction Modal workspace, Modal SDK 1.6.1, 2026-10-10. No mocks. All sandboxes were
terminated; secret-shaped values never left the build host.

| File | What it shows |
| --- | --- |
| `pr1-vm-smoke.mp4` / `.png` | Live terminal recording of `make smoke-modal`: image prewarm, VM allocation, kernel proof, a DeepSeek custom-API Turn with the official OpenCode CLI, termination, and zero sandboxes left running. |
| `vm-smoke.json` | Sanitized report of that recorded run. |
| `image-cold-build.json` | Cache-busted build of the full shared image (98.5 s) versus a cached resolve (1.2 s). |
| `mvp-acceptance-evidence.json` | `tests/e2e_modal/mvp_acceptance.py`: 16/16 gates through the real API, worker, Modal VMs and GitHub test repository, with the built-in secret scan and cleanup. |
| `acceptance-provisioning.json` | `executor.bound.provisioning` for the three acceptance leases, Job attempt outcomes (no `claim_expired`) and the `connection.provision` result. |

The screen recording is a browser capture of a page that streams the command's stdout as it runs.
