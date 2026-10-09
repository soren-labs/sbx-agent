# Phase One PR 1 evidence — generic inference and five official CLIs

Tested commit: `e8a7e23`. Environment: non-production staging on the AWS build host
(control plane `:8800`, Console dev server `:5174`, PostgreSQL 16), real Google Chrome 154
driven by Playwright with a throwaway profile per test user. Real DeepSeek key, real Modal
workspace sandboxes, real GitHub token. No credential appears in any artifact: secret inputs
are password fields, and screenshots were taken after token fields were cleared.

## Checks

| Command | Result |
| --- | --- |
| `make lint` | pass |
| `make test` | 382 passed, 1 skipped |
| `make console-check` | typecheck, 50 Vitest tests, production build pass |
| `make docs-check` | 109 pages built, links ok |

## Per-CLI real runs (DeepSeek `deepseek-flash`)

Each run is two Turns in one Session: (1) create a file and run a shell command, (2) recall
without reading files, which only succeeds through the CLI's native session resume.
Raw API results: [api-smoke-results.json](api-smoke-results.json).

| Harness | Official CLI | Protocol used | Local executor (API) | Modal sandbox (API) | Modal sandbox (Console UI) |
| --- | --- | --- | --- | --- | --- |
| OpenCode | opencode-ai 1.18.35 | openai_chat | pass, resumed | pass, resumed | pass |
| Codex | @openai/codex 0.162.0 | openai_responses | pass, resumed | pass, resumed | pass |
| Claude Code | @anthropic-ai/claude-code 2.1.295 | anthropic_messages | pass, resumed | pass, resumed | pass |
| Grok Build | @xai-official/grok 1.0.50 | openai_chat | pass, resumed | pass, resumed | pass |
| Command Code | command-code 1.79.2 | openai_chat | pass, resumed | pass, resumed | pass |

All five emitted tool events, a native session binding and token usage.

## Browser evidence

New user flow (register → verify → sign in → Connections):

| Step | Screenshot |
| --- | --- |
| Registration submitted | [01](01-register-submitted.png) |
| Email verified | [02](02-email-verified.png) |
| Home with setup checklist | [03](03-home-empty-setup.png) |
| Connections, nothing configured | [04](04-connections-empty.png) |
| Error state: provider rejects the key (per-endpoint 401) | [05](05-inference-rejected-key.png) |
| Disconnect needs confirmation | [06](06-disconnect-confirm.png) |
| Client validation of a base URL | [07](07-inference-bad-url.png) |
| Inference form, key masked | [08](08-inference-form-filled.png) |
| Inference key validated | [09](09-inference-ready.png) |
| Inference + Modal + GitHub ready | [10](10-connections-all-ready.png) |
| Home after setup | [11](11-home-ready.png) |
| Mobile 390px | [12](12-mobile-connections.png) |
| Light theme | [desktop](light-connections.png), [home](light-home.png), [mobile](light-mobile-connections.png) |

Session creation and chat (Modal sandbox), per CLI — picker, first Turn with tool output,
follow-up Turn answered from resumed context:

| CLI | Picker | Turn 1 | Turn 2 |
| --- | --- | --- | --- |
| OpenCode | [21](21-opencode-picker-selected.png) | [25](25-opencode-turn1-succeeded.png) | [26](26-opencode-turn2-succeeded.png) |
| Codex | [21](21-codex-picker-selected.png) | [25](25-codex-turn1-succeeded.png) | [26](26-codex-turn2-succeeded.png) |
| Claude Code | [21](21-claude-picker-selected.png) | [25](25-claude-turn1-succeeded.png) | [26](26-claude-turn2-succeeded.png) |
| Grok Build | [21](21-grok-picker-selected.png) | [25](25-grok-turn1-succeeded.png) | [26](26-grok-turn2-succeeded.png) |
| Command Code | [21](21-commandcode-picker-selected.png) | [25](25-commandcode-turn1-succeeded.png) | [26](26-commandcode-turn2-succeeded.png) |

Also: [Session options](22-opencode-options.png), [Session starting](24-opencode-session-starting.png),
[Activity tab](27-opencode-activity.png), [Files tab](27-opencode-files.png),
[mobile Session](28-claude-mobile-session.png).

Video (real Claude Code Session on Modal, picker → task → tool output → follow-up):
[claude-code-session.mp4](claude-code-session.mp4).

## Known limits

- Command Code's headless entrypoint requires *a* Command Code account key to be present even
  in its documented `--local-only` BYOK mode. The adapter supplies a fixed non-secret
  placeholder that local-only mode never transmits; no Command Code account is used.
- `mcp`, `skills`, `attachments` and `effort_settings` are declared `unknown` for every
  Harness: the CLIs have them, SBX does not wire them yet.
