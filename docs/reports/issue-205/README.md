# Issue #205 evidence: Antigravity and Grok Build machines

Staging: an isolated nonproduction stack on the build host (PostgreSQL, control plane, Console dev
server) with the nonproduction Modal workspace. Every live result below ran on 2026-10-11 against
real Modal `runtime="vm"` sandboxes, the real provider endpoints, and two machines whose official
logins a person approved in their own browser. No result was produced with an API key standing in
for a subscription: every subscription Session below has `connections.inference = null`.

Nothing was deployed.

## Result per requirement

| # | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Three providers; independent Slots, Volumes and homes per provider and account | PASS | automated tests; live: one Volume per machine in `live/*.json` |
| 2 | Grok Build official login to Ready, from the Console | PASS | `live/console-flow-grok.json`, `shots/live-grok-02-login-waiting.png`, `shots/live-grok-05-ready.png` |
| 2 | Antigravity official sign-in with a pasted code to Ready, from the Console | PASS | `live/console-flow-antigravity-first-run.json` (`final: ready`), `shots/live-antigravity-03-code-pasted-masked.png`, `shots/live-antigravity-05-ready.png` |
| 2 | Rejected code, cancel, delete clean up | PASS | made-up code ends `code_rejected` at 107 s; `login-panels.json` (`after_cancel`, `deleted`) |
| 2 | Login expiry | PASS by automated test only | `test_terminal_sign_in_expires_when_no_code_arrives`, `test_denied_and_expired_logins_need_login_and_can_retry` |
| 2 | Stored login works on a brand-new VM with nobody signing in | PASS, both providers | `live/live-*.json` `verify_on_fresh_vm`: `real_model_call: true`; `second_vm.different_vm_than_session_1: true` |
| 3 | Real noninteractive Turn with the official binary, file write plus shell command | PASS, both providers | `live/live-*.json` `task`: `succeeded`, 2 tool calls, the answer contains the file's word; real token usage |
| 3 | Resume in the same Session | PASS, both providers | `resume`: `succeeded`, `native_resumed: true`, the model recalls the word without tools |
| 3 | Cancel a running Turn | PASS, both providers | `cancel`: `running` then `cancelled / cancelled_by_user` in 14 s; machine `ready` afterwards |
| 4 | Catalog and effort from the signed-in CLI | PASS | Grok: 4 models with per-model effort menus. Antigravity: 14 models as printed, no separate effort (see below) |
| 4 | Server refuses invented models and unlisted efforts | PASS | `refusals` in `live/live-*.json`; `live/cli-flow.log` |
| 5 | Console: status, details, model and effort pickers; desktop and 320 px; English and Chinese | PASS | `live/ready-shots.json` (every capture `fits_width: true`, no page errors), `shots/*picker*`, `shots/*machines-ready*` |
| 5 | Real task started from the Console on Antigravity | PASS | `live/console-task-antigravity.json`, `live/console-task-antigravity.mp4`, `shots/live-antigravity-07-task-result.png` |
| 5 | Codex machine and custom API key regression | PASS | `live/regress.json`: both answered on new VMs |
| 6 | SDK and CLI workflow on staging | PASS | `live/cli-flow.log` |
| 7 | Zero orphaned sandboxes | PASS after every run | `modal.Sandbox.list` on the executor app: 0 live |

## The Antigravity failure and its fix

The first real Antigravity task, started from the Console, failed
(`live/console-flow-antigravity-first-run.json`):

```
invalid model selection (--model "gemini-3.8-flash-high" --effort "low"):
--model gemini-3.8-flash-high conflicts with --effort=low
```

Before any login had been approved, the catalog parser and the effort picker were built on the
CLI's `--help` text. Running the signed-in CLI in a VM on the machine's own Volume showed the real
behaviour (`agy` 1.3.3):

| Command | Result |
| --- | --- |
| `agy models` | `<slug><TAB><display name>` per line; one slug per effort level, e.g. `...-high`, `...-medium`, `...-low` |
| `--model <variant>-high --effort low` | exit 1, `conflicts with --effort=low`, no model call |
| `--model <variant>-high --effort high` | success |
| `--model <variant>-low` | success |
| `--model claude-sonnet-4-6 --effort low` | exit 1, `--effort is not supported for model` |
| no `--model`, no `--effort` | success |

Fix: the catalog keeps each printed slug with its display name and no effort list, the server
rejects any effort on an Antigravity machine, the Harness never passes `--effort`, and the Console
shows the variants by name with no effort picker. A recorded real `stream-json` run is now the test
fixture for the Harness (`tests/fixtures/harnesses/antigravity/success.jsonl`).

After the fix the same machine ran real tasks from the API, the CLI and the Console.

## Live numbers

| | Grok Build | Antigravity |
| --- | --- | --- |
| CLI in the VM image | `grok 1.0.50` | `agy 1.3.3` |
| Login check on a fresh VM | 18 s, real model call | 28 s, real model call |
| Models reported | 4 | 14 |
| File + shell task | succeeded, 18 s | succeeded, 22 s |
| Resume Turn | succeeded, 6 s | succeeded, 5 s |
| Task on a second fresh VM | succeeded, different boot id | succeeded, different boot id |
| Cancel | cancelled in 14 s | cancelled in 14 s |

## Known limits

- Antigravity's usage for a resumed Turn looks cumulative for the conversation (the resumed Turn
  reported more input tokens than the first). SBX stores what the CLI reports; per-Turn usage for
  resumed Antigravity Turns may therefore be overstated. Not fixed here.
- Antigravity reports `cached_input_tokens: 0`; the CLI printed no cache reads in these runs.
- Login expiry was not waited out on a real VM (15 minutes); it is covered by automated tests.
- `AGY_CLI_DISABLE_AUTO_UPDATE` is taken from the binary, not from public documentation. The image
  pins 1.3.3 and the VMs reported 1.3.3 throughout.

## Found and fixed during browser runs

Grok Build prints its sign-in link with the one-time code in the query. The Console printed the
whole link as text, so the code was readable outside the blurred element. The Console now prints
only the link's origin and path. The first captures were deleted; the capture scripts refuse to
take a frame if a code or a query is visible elsewhere on the page.

## Screenshots and video

All from the real staging Console. Device codes and the pasted-code field are blurred, and each
script proves the blur is applied before a frame is taken. The sign-in link is never printed.

| File | What it shows |
| --- | --- |
| `shots/live-grok-02-login-waiting.png` | Grok Build device-code step of the login that was approved |
| `shots/live-grok-05-ready.png`, `shots/live-grok-07-task-result.png` | Ready, and the first real Grok task |
| `shots/live-antigravity-03-code-pasted-masked.png` | The pasted code in the masked field before sending |
| `shots/live-antigravity-05-ready.png` | Both machines Ready |
| `shots/live-antigravity-06-model-picked.png`, `shots/live-antigravity-07-task-result.png` | Antigravity variant picked with no effort picker; the real task result |
| `shots/*-picker-antigravity.png`, `shots/*-picker-grok.png` | Model and effort pickers, desktop and 320 px, English and Chinese |
| `shots/desktop-en-machines-ready.png`, `shots/mobile320-zh-machines-ready.png` | Machines page with both Ready |
| `shots/*-login-waiting.png`, `shots/*-add-provider-menu.png` | Login panels and the add menu from the earlier run |
| `live/console-task-antigravity.mp4` | The Antigravity task from the Console, downscaled |

The full login recordings stay on the build host (`~/sbx-issue205/evidence`, 24 MB and 87 MB).

## Checks

See the pull request description for the results at the final commit.
