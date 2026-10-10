# PR 4 evidence: SDK/CLI and Console "My Cloud Machines"

Real Console (real Google Chrome via Playwright) against a real nonproduction stack: control plane,
PostgreSQL, Modal VMs (Modal SDK 1.6.1) and the official Codex CLI 0.162.0, 2026-10-10. No API
route was intercepted or stubbed. The one-time device code is blurred in every frame (the scripts
assert the blur before any capture) and is not in any file here. Nothing read a profile Volume.

| File | What it shows |
| --- | --- |
| `pr4-device-login.mp4` | Add Codex machine -> real sign-in URL and code -> a person approves on the official page -> the row turns Ready by itself -> its real models and efforts -> a real task -> Ready again. The 164 s spent waiting for the person is played at 16x; the rest is real time. |
| `login-flow.json` | Report of that run: Volume, CLI version, catalog source, 7 models with per-model efforts, the Session's model/effort, `code_still_returned: false`. |
| `login-01-*` ... `login-07-*` | Stills: pending (desktop and Android 320 px, code masked, panel scrolled into view), verifying, Ready, model/effort picker, task result, Ready again. |
| `pr4-session-on-machine.mp4`, `session-on-machine-*.png` | A Session on the previously approved machine A1: picker, Running, result. |
| `machines-android-320-*`, `machines-android-360-light-zh.png` | 19 machines as compact collapsed rows at 320 px (dark and light) and 360 px in Chinese; horizontal overflow measured 0 px. |
| `machines-desktop-*` | Desktop dark/light, expanded Ready and Needs-login rows, status filter, search. |
| `new-session-*` | New Session: only runnable machines are offered (15 others are one link away), the selected machine's real catalog, a model with a different effort list, and DeepSeek's On/Off thinking switch. |
| `cli-real-run.txt` | `sbx slots models`, a refused effort (422 with the model's own list), and a real `sbx sessions create --slot --model --effort` Turn. |

Facts from these runs:

- New machine "Codex 19": login approved by a person, verified with a real model call, Volume
  `modal_volume_v2`, catalog source `codex app-server model/list`, 7 models. Two of them do not
  list `ultra`; the picker and the server both follow that.
- Console Session on it: `gpt-6.1-sol`, effort `low`, subscription mode, succeeded in 36 s.
- CLI Session on it: `gpt-6-luna`, effort `high`, succeeded; the machine showed `running`, then
  `ready` after release. `--effort ultra` for that model was refused before any VM started.
- 19 machines: 4 ready, 15 needing a login (real empty Volumes checked on real Setup VMs).

Scripts used (outside the repository): `shots.mjs`, `session.mjs`, `login-flow.mjs`,
`scroll-check.mjs`. The two machines created by `scroll-check.mjs` were cancelled and deleted.
