# PR 5 evidence: full Console E2E on staging, and the #199 acceptance map

Real Google Chrome (Playwright) → real Console → real control plane and PostgreSQL → real Modal
VMs (Modal SDK 1.6.1) → official OpenCode and Codex CLIs, 2026-10-10, nonproduction. No API route
was intercepted. Nothing read a profile Volume.

## This run: `pr5-console-e2e.mp4` (90 s, real time), `e2e-console.json` (9/9 gates)

| Gate | Result |
| --- | --- |
| Custom API key (DeepSeek, OpenCode, thinking Off) Session succeeds on a Modal VM | pass, `Linux 7.2.9`, no machine attached |
| Two Codex machines (A1, A2: one account, two logins) show Running at the same time | pass |
| Busy machines are listed as "in use" and cannot be picked | pass |
| Both machine Sessions succeed in subscription mode with no API key | pass |
| The three Sessions ran in three different VMs (three boot IDs) | pass |
| Each machine Session used its own model and effort (`gpt-6-astra`/`low`, `gpt-6-sol`/`medium`) | pass |
| Machines are Ready again after release | pass |
| Delete needs the typed name and removes only that machine | pass (18 → 17) |

Stills `e2e-01` … `e2e-08` follow the same order. After the run: 0 sandboxes running in the Modal
workspace. Thinking Off is shown by the Session's pinned `effort: "none"`; its effect on reasoning
tokens was measured in PR 3, not here.

Secrets sweep of this staging (counts only): the device code approved in PR 4, the Modal token
secret, the DeepSeek key and any JWT-shaped value appear 0 times in the control-plane log and 0
times in a plaintext dump of the database; 0 login attempts still store a code.

## Where each #199 requirement is shown

| Requirement | Evidence |
| --- | --- |
| One Modal VM backend for custom API and subscriptions | PR 1 `pr-01/` (16/16 acceptance), this run (gate 1 and 5) |
| One login = one Slot = one private v2 Volume; one Worker per Slot | PR 2 `pr-02/` (17/17), PR 3 `pr-03/` (30/30) |
| Many Slots in parallel; one account on two independent Slots | PR 3 rounds A1+A2, A1+B1, A1+A2+B1; this run (A1+A2 from the Console) |
| Device login in the user's Setup VM, real URL and code, automatic Ready, no save step | PR 4 `pr-04/pr4-device-login.mp4` (approved by a person) |
| Re-login, sign out, cancel, delete, crash cleanup | PR 2 acceptance and tests; cancel and delete from the Console in PR 4 and this run |
| 15–20 machines, Android 320 px and desktop, light/dark | PR 4 `machines-android-320-*`, `machines-desktop-*` |
| Real model catalog, per-model efforts, console → API → CLI | PR 3 (catalog source, effort changes reasoning tokens), PR 4 pickers and `cli-real-run.txt` |
| DeepSeek thinking control only where it works | PR 3 `deepseek-*.json`, PR 4 `new-session-deepseek-thinking-switch.png`, this run |
| SDK and CLI | PR 4 `cli-real-run.txt`, `tests/integration/sdk/test_sdk_cli.py` |
| Public documentation | `docs-site` guide "Cloud machines" and reference pages (this PR) |

## Not shown by a live run

- Sign out and log in again were exercised through the API on real VMs (PR 2), not recorded in the
  browser.
- Thinking control for Claude Code, Grok Build and Command Code with a custom API is not offered,
  because it was not verified.
- Nothing was deployed to production.
