---
title: Providers
description: Which official provider CLIs are supported, experimental or disabled, and their capability tiers.
---

Harness support is declared in pinned manifests. The machine-readable table is
[provider-reference.json](/provider-reference.json).

| Provider | Official CLI (pinned) | Support tier | Inference protocols accepted |
| --- | --- | --- | --- |
| `opencode` | OpenCode — `opencode-ai` 1.18.35 | supported | `openai_chat`, `anthropic_messages`, `openai_responses` |
| `codex` | Codex — `@openai/codex` 0.162.0 | supported | `openai_responses` |
| `claude` | Claude Code — `@anthropic-ai/claude-code` 2.1.295 | supported | `anthropic_messages` |
| `grok` | Grok Build — `@xai-official/grok` 1.0.50 | supported | `openai_chat` |
| `commandcode` | Command Code — `command-code` 1.79.2 | supported | `openai_chat`, `anthropic_messages`, `openai_responses` |
| `devin` | — | disabled | n/a |
| `antigravity` | — | disabled | n/a |

Every supported Harness runs the vendor's own CLI with an `inference_api`
Connection (bring your own key); the Harness and the model provider are
independent. Vendor subscription logins are not used.

A disabled provider cannot be selected. Capability values are `supported`,
`unsupported` or `unknown`, and `unknown` is never treated as supported. For all
five supported Harnesses `event_stream`, `native_resume`, `native_state_export`
and `usage` are supported and verified with real Turns; `interrupt`, `steer`,
`interactive_approval`, `model_discovery` (models come from the Connection) and
`credential_writeback` are not. `mcp`, `skills`, `attachments` and
`effort_settings` are `unknown`: the CLIs have them, SBX does not wire them yet.

Notes per CLI:

- **Codex** speaks only the OpenAI Responses API, so the Connection needs an
  `openai_responses` endpoint.
- **Claude Code** speaks only Anthropic Messages (`ANTHROPIC_BASE_URL`); no
  Claude subscription login is involved.
- **Command Code** runs in its documented local-only BYOK mode
  (`--local-only`): no Command Code account or backend traffic.

Use `GET /api/harnesses` for the installed catalog, `GET /api/models` for
Connection-scoped model availability and `GET /api/executor-backends` for
executors. These reads have no side effects.

A Harness switch creates a new linked Session with an explicit summary Message.
SBX never silently translates hidden context between CLIs.
