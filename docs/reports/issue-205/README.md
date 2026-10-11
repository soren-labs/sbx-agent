# Issue #205 evidence: Antigravity and Grok Build machines

Staging: an isolated nonproduction stack on the build host (PostgreSQL, control plane, Console dev
server) with the nonproduction Modal workspace. Everything below ran on 2026-10-11 against real
Modal `runtime="vm"` sandboxes and the real provider endpoints. Nothing was merged or deployed.

**No real subscription login has been approved yet for either provider.** Every step that needs a
person to sign in is marked NOT VERIFIED. No result below was produced with an API key standing in
for a subscription.

## Result per requirement

| # | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Three providers listed; independent Slots, Volumes and homes per provider and account | PASS (automated tests; real VMs up to the sign-in step) | `test_three_providers_each_with_its_official_login_method`, `test_providers_and_accounts_never_share_a_volume_or_a_home`; `login-panels.json` (one Volume per Slot) |
| 2a | Grok Build official login reaches the approval step in a real VM | PASS | `login-panels.json`: `accounts.x.ai`, device code present, 6-8 s |
| 2b | Antigravity official sign-in reaches the paste-code step in a real VM | PASS | `login-panels.json`: `accounts.google.com`, `input=authorization_code`, 6-8 s |
| 2c | A code the provider does not accept ends as `code_rejected`, VM removed | PASS | probe on staging with a made-up code: `verifying` at 13 s, `needs_login / code_rejected` at 107 s |
| 2d | Cancel, delete, expiry clean up the VM and Volume | PASS for cancel and delete on real VMs; expiry by automated test only | `login-panels.json` (`after_cancel`, `deleted`), zero live sandboxes afterwards |
| 2e | A real approved login turns Ready and persists into a new VM | **NOT VERIFIED** (needs a person) | |
| 3 | Real noninteractive Turn with the official `agy` / `grok` binary on a subscription | **NOT VERIFIED** (needs 2e) | Harness behaviour is covered by unit tests only |
| 4 | Model and effort catalog from the authenticated CLI | **NOT VERIFIED** for real logins. Grok's catalog shape and `agy`'s effort levels were read from the real binaries signed out | `tests/unit/integrations/test_subscription_adapters.py` |
| 5 | Console: add, login status, grouping, pickers; English and Chinese; desktop and 320 px | PASS for add menu, both login panels, cancel; pickers by Vitest only until a machine is Ready | `shots/`, `login-panels.json` (`fits_width: true` in every view, no page errors) |
| 5 | Codex and custom API regression | PASS | `regress.json`: Codex on an adopted test Volume and DeepSeek through OpenCode both answered on new VMs |
| 6 | API, SDK, CLI, OpenAPI, manifests, docs-site, changelog, example | PASS (checks below) | |
| 7 | Zero orphaned sandboxes | PASS after every run above | `modal.Sandbox.list` on the executor app: 0 live |

## Checks

| Check | Result |
| --- | --- |
| `make lint` | pass |
| `make test` | 484 passed, 1 skipped |
| `make console-check` | typecheck pass, 99 tests passed, build pass |
| `make docs-check` | pass (121 pages, links ok) |

## Found and fixed during the browser run

Grok Build prints its sign-in link with the one-time code in the query
(`.../oauth2/device?user_code=...`). The Console printed the whole link as text under the button,
so the code was readable outside the element that captures blur. The Console now prints only the
link's origin and path; the button still opens the provider's own link. The first captures were
deleted, the logins they belonged to had already been cancelled, and the capture script now refuses
to take a frame if a code or a query is visible anywhere else on the page.

## Screenshots

All taken from the real staging Console. Codes are blurred, and the script proves the blur is
applied before each frame.

| File | What it shows |
| --- | --- |
| `shots/desktop-en-add-provider-menu.png` | Add machine menu with the three providers |
| `shots/mobile320-zh-add-provider-menu.png` | The same at 320 px in Chinese |
| `shots/desktop-en-grok-login-waiting.png` | Grok Build device-code step, real URL host, code blurred |
| `shots/desktop-en-antigravity-login-waiting.png` | Antigravity sign-in step with the paste field |
| `shots/mobile320-zh-grok-login-waiting.png` | Grok Build step at 320 px in Chinese |
| `shots/mobile320-zh-antigravity-login-waiting.png` | Antigravity step at 320 px in Chinese |
| `shots/mobile320-en-machines.png` | Machines page at 320 px |
| `shots/desktop-zh-new-session-pickers.png` | New Session pickers in Chinese |

## What a person has to do to finish 2e, 3 and 4

One browser approval per provider, with an account that has the subscription:

1. Grok Build: open the `accounts.x.ai` link the machine shows and confirm the code.
2. Antigravity: open the Google link the machine shows, sign in, and paste the code the page
   displays back into the machine.

The recorded flow script then continues by itself: Ready, catalog, a real task on a new VM,
release, and the orphan check.
