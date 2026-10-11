# Issue #205 progress and handoff

Antigravity (`agy`) and Grok Build (`grok`) subscription Machine Slots, on top of the merged Codex
implementation (#200-#204). Nothing here has been merged or deployed.

| PR | Branch | Scope | State |
| --- | --- | --- | --- |
| 1 | `feat/issue-205-provider-auth-core` | adapters, login methods, Setup VM supervisor, API, spec | draft, https://github.com/soren-labs/sbx-agent/pull/206 |
| 2 | `feat/issue-205-execution-catalog` (stacked on 1) | `agy` Harness, Grok subscription mode, catalog, effort | draft, https://github.com/soren-labs/sbx-agent/pull/207 |
| 3 | `feat/issue-205-console-sdk-cli` (stacked on 2) | Console, SDK, CLI | draft, https://github.com/soren-labs/sbx-agent/pull/208 |
| 4 | `feat/issue-205-e2e-docs` (stacked on 3) | staging E2E evidence, docs-site, changelog | draft |

## Verified on the real CLIs (2026-10-11, isolated HOME, no credentials)

| | Grok Build `grok` 1.0.50 | Antigravity `agy` 1.3.3 |
| --- | --- | --- |
| Subscription login without a browser | `grok login --device-auth`: prints `https://accounts.x.ai/oauth2/device?user_code=...` and an `XXXX-XXXX` code, then polls | no login subcommand; first-run screen "Google OAuth" prints an `accounts.google.com/o/oauth2/auth` URL (redirect `antigravity.google/oauth-callback`) and asks to paste the authorization code |
| Login storage | `$GROK_HOME/auth.json` (default `~/.grok`) | OS keyring; changelog: bypassed for file storage when no D-Bus session bus exists |
| Signed-out answer | `grok models`: "You are not authenticated." (exit 0); `grok -p`: exit 1 with a JSON error | `agy models`: "Please sign in" (exit 0); `agy -p` without a terminal did not exit within 40 s |
| Headless run | `-p`, `--output-format plain\|json\|streaming-json\|streaming-messages-json` | `-p`, `--output-format text\|json\|stream-json` |
| Model catalog | `grok agent stdio` `initialize` -> `_meta.modelState.availableModels[]` with `reasoningEfforts[]` (JSON) | `agy models` (text; format not seen while signed out) |
| Effort | `--effort` / `--reasoning-effort`, per-model menu | `--effort low\|medium\|high\|xhigh\|max` (binary help; docs list low/medium/high) |

## Not verified yet (needs one real login per provider)

- Antigravity: that the login lands on the Slot Volume under `HOME=/profile` in a VM without D-Bus,
  and survives into a new VM; whether the sign-in screen asks anything after the code is entered.
- Antigravity: the authenticated `agy models` output format (the catalog parser depends on it).
- Grok: the authenticated `grok models` header (`status_ok` is "Available models", seen signed out)
  and the subscription catalog from `grok agent stdio`.
- Both: a real model call on a new Modal `runtime=vm` Worker.

## PR 1 notes

- `runtime/subscriptions/setup.py`: `login_pty` drives a full-screen sign-in under a PTY 4000 columns
  wide (the URL then prints on one line), types the submitted code once, keeps terminal output in
  memory only. `status_fail` lets a CLI's signed-out sentence override exit code 0.
- `POST /api/machine-slots/{id}/logins/current/code`: synchronous relay through
  `ModalExecutor.setup_input` (stdin of an exec); the code is never persisted.
- `agy` is installed from the vendor's release archive, SHA-512 pinned in
  `protocol/capabilities.py` (`HARNESS_CLI_ARCHIVES`); bumping it means copying the new entry from
  `https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json`
  (the URL the official `install.sh` reads).
- `AGY_CLI_DISABLE_AUTO_UPDATE` is set from the name found in the binary; it is not in the public docs.

## PR 2 notes

- Catalog sources: Grok from `grok agent stdio` `initialize` (nested path, generic in the
  supervisor); Antigravity from the printed lines of `agy models` plus the `--effort` levels in
  `agy --help`. The Antigravity parser accepts a line only when its first word is a lower-case
  slug; if the real output names models differently the catalog is `unavailable` and Sessions run
  the provider default. Check it against a real login and adjust `parse_catalog`.
- `runtime/harnesses/antigravity.py` is new and subscription-only. The stream shape follows the
  public headless docs (`event` discriminator, payload under the same key); no recorded fixture
  from a real run exists yet. `tool_info` output field names are a guess (`output` / `result`).
- Grok subscription Turns keep the existing `streaming-messages-json` normalizer; only the login
  location, `-m` and `--effort` differ from the custom-API mode.
- `antigravity` left the disabled Harness list: the archive is checksum-pinned, which was the gate.
- `tests/unit/runtime/test_startup_receipts.py::test_service_receipt_mutation_cannot_confirm_orphan_stop[True-unlink]`
  failed once in a full run and passed 4 of 4 reruns; the file is untouched by this work.

## PR 3 notes

- Console: "Add machine" becomes a provider menu once more than one provider is available; a
  provider gets a group only when it has a machine. The Antigravity login shows the sign-in host
  (not the 700-character URL), a paste field (`data-testid="login-code-input"`) and "Send code".
  Blur that field before any capture, like `device-code`.
- New Session offers a Harness with no custom-API mode only through a machine, and shows a note
  when the catalog's `effort_scope` is `cli`.
- SDK `client.slots.submit_code`, `wait(on_input=...)`; CLI `sbx slots code`. `sbx sessions create
  --slot` now takes the Harness from the Slot's provider (it was hardcoded to codex). The SDK/CLI
  glue and its tests were written by a Haiku subagent and reviewed.
- Browser checks (desktop, 320 px, zh-CN) are still to be done on staging; only Vitest covers the
  new UI so far.

## PR 4 notes and current state (2026-10-11)

Evidence and the requirement map: `docs/reports/issue-205/README.md`.

Verified on staging with real Modal VMs: both official logins reach the step where a person must
act (Grok: `accounts.x.ai` link and device code; Antigravity: Google link and paste field), a
made-up Antigravity code ends as `code_rejected`, cancel and delete remove the VM and the Volume,
Codex on a Slot and a custom API key still work on the new image, zero live sandboxes afterwards.

**Still not verified, blocked on one browser approval per provider:** a real login turning Ready,
persistence into a new VM, the authenticated catalogs, and a real Turn on `grok` and `agy`.

To finish, with the staging stack up (`~/sbx-issue205/stage/up.sh`):

1. `cd ~/sbx-issue205/pw && node flow.mjs grok "Grok Build" ~/sbx-issue205/evidence/flow-grok`
   The person reads the link and code from `~/sbx-issue205/stage/pending-login.json` (mode 0600,
   deleted when the attempt ends) and approves within 15 minutes.
2. `node flow.mjs antigravity "Antigravity" ~/sbx-issue205/evidence/flow-antigravity`
   The person opens the link from the same file, signs in, and saves the code the page shows as one
   line in `~/sbx-issue205/stage/pasted-code`; the script types it into the blurred field.
3. Each run writes `flow.json` (no codes, no URL queries), screenshots and `flow.webm`, runs one
   real task on a new VM and releases it. Then `uv run python ~/sbx-issue205/stage/orphans.py`.
4. Check `flow.json` `slot.catalog` for Antigravity. If `status` is `unavailable`, read the real
   `agy models` output on the Slot and adjust `parse_catalog` in
   `control/integrations/subscriptions/antigravity.py`.

Staging files outside the repository: `~/sbx-issue205/stage` (`up.sh`, `seed.py`, `probe_login.py`,
`regress.py`, `orphans.py`, `user.json`), `~/sbx-issue205/pw` (`lib.mjs`, `shots.mjs`,
`login-panels.mjs`, `flow.mjs`), raw captures in `~/sbx-issue205/evidence`. Ports: PostgreSQL 54405,
control plane 8815, Console 5185. No Slots are left on staging.

Docs-site pages, both changelogs and `examples/subscription_machines.py` were drafted by a Haiku
subagent from a fact sheet and reviewed; the example has not been run against a live API.
