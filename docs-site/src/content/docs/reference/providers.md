---
title: Providers
description: Which official provider CLIs are supported, experimental or disabled, and their capability tiers.
---

Harness support is declared in pinned manifests. The machine-readable table is
[provider-reference.json](/provider-reference.json).

| Provider | Support tier | Credentials |
| --- | --- | --- |
| `opencode` | supported | `opencode_zen` Connection |
| `codex` | experimental | optional `codex` Connection |
| `claude` | disabled | n/a |
| `devin` | disabled | n/a |
| `grok` | disabled | n/a |
| `antigravity` | disabled | n/a |

A disabled provider cannot be selected. Capability values are `supported`,
`unsupported` or `unknown`, and `unknown` is never treated as supported. For
OpenCode, `event_stream`, `model_discovery`, `native_resume`, `native_state_export`
and `usage` are supported; `interrupt`, `steer`, `interactive_approval` and
`credential_writeback` are not.

Use `GET /api/harnesses` for the installed catalog, `GET /api/models` for
Connection-scoped model availability and `GET /api/executor-backends` for
executors. These reads have no side effects.

A Harness switch creates a new linked Session with an explicit summary Message.
SBX never silently translates hidden context between CLIs.
