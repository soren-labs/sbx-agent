# PR 2 evidence: Machine Slots and the official Codex device login

Real API, real PostgreSQL and real Modal Setup VMs (nonproduction workspace, Modal SDK 1.6.1,
Codex CLI 0.162.0), 2026-10-10. Nothing read a profile Volume; only the official CLI inside each VM
touched a login. The one-time device code is masked.

| File | What it shows |
| --- | --- |
| `pr2-slots.mp4` / `.png` | Live terminal recording of `tests/e2e_modal/slots_acceptance.py`. |
| `slots-acceptance.json` | Sanitized report of that run: 17/17 gates. |

Covered: three Volumes holding user-approved logins (two for one account, one for another) each
become an independent `ready` Slot after a real model call on a fresh VM; a new Slot starts the
official device login and returns the genuine URL, a code and a 15-minute expiry in about 6 s;
a second login is refused while one is live; cancel stops the Setup VM; deleting a managed Slot
deletes its Volume while adopted Volumes are preserved; no VM is left running.

Not covered here: a person approving a brand-new code. `SBX_SLOT_LOGIN=approve` runs that path.
