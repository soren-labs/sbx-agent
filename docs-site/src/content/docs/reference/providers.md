---
title: Providers
description: Which official provider CLIs are supported, which are disabled, how subscription providers sign in, and their capability tiers.
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
| `antigravity` | Antigravity — `agy` 1.3.3 (release archive) | supported, subscription machines only | none |
| `devin` | — | disabled | n/a |

Most supported Harnesses run the vendor's own CLI with an `inference_api`
Connection (bring your own key); the Harness and the model provider are
independent. Three of them can also run on your own subscription login through a
[cloud machine](/guides/cloud-machines/): Codex, Grok Build and Antigravity. The
other CLIs use API keys only. Antigravity has no API-key mode, so it runs only on
a cloud machine.

## Subscription providers

A subscription provider signs in with its official CLI inside a temporary VM in
your own Modal workspace. Each machine is one login on its own private Volume.
SBX never reads or returns the login and never falls back to an API key.

| Provider | Official CLI | Login method | Sign-in host | Model catalog source | Reasoning effort | Custom API key mode |
| --- | --- | --- | --- | --- | --- | --- |
| `codex` | `codex` (`@openai/codex`) | Device code: a URL and a one-time code | `auth.openai.com` | `codex app-server model/list`, as the CLI reports it | Per model, from each model's `supportedReasoningEfforts` | Yes (`openai_responses`) |
| `grok` | `grok` (`@xai-official/grok`) | Device code: a URL and a one-time code | `accounts.x.ai` | `grok agent stdio initialize`, as the CLI reports it | Per model, from each model's effort menu; some models have none | Yes (`openai_chat`); no reasoning effort in this mode |
| `antigravity` | `agy` | Authorization code: a Google sign-in link, then the code the browser shows, pasted back into SBX | `accounts.google.com` | `agy models`: the ids and names as printed, with no machine-readable catalog. If the list cannot be read, only the provider default is used | None as a separate setting. The CLI lists one model per effort level and refuses `--effort` next to it, so the level is chosen with the model | No |

Notes for each subscription provider:

- **Codex** and **Grok Build** show a sign-in URL and a one-time code. The user
  opens the URL in a browser and confirms the code there.
- **Antigravity** has no device code. The user opens the Google sign-in link in a
  browser and signs in with the Google account. The browser then shows a code.
  The user pastes that code into SBX (Console, `sbx slots code`, or
  `client.slots.submit_code`). SBX sends it to the waiting CLI and does not store
  it. A rejected code leaves the machine at `needs_login` with reason
  `code_rejected`; a new login gives a new link.
- The catalog is always the CLI's own answer for that login. Hidden models are
  dropped, and no model names, plans or quotas are added by SBX.

## Capabilities

Capability values are `supported`, `unsupported` or `unknown`, and `unknown` is
never treated as supported. For the five Harnesses that were verified with real
Turns on API keys (OpenCode, Codex, Claude Code, Grok Build and Command Code),
`event_stream`, `native_resume`, `native_state_export` and `usage` are supported.
`interrupt`, `steer`, `interactive_approval` and `credential_writeback` are not
supported for those five. The exact per-provider values are in
[provider-reference.json](/provider-reference.json). The subscription providers
are described in the [cloud machines guide](/guides/cloud-machines/).

`model_discovery` is supported for Codex, Grok Build and Antigravity on a cloud
machine (the CLI's own catalog). Elsewhere models come from the Connection.
`effort_settings` is supported for Codex and Grok Build on a cloud machine,
where the levels come from the selected model. Antigravity has no separate
effort setting; its models are effort variants. It is also supported for OpenCode with an API key (thinking off, where
the provider's switch was verified). It is not wired for the other CLIs. `mcp`,
`skills` and `attachments` are `unknown`: the CLIs have them, SBX does not wire
them yet.

Notes per CLI:

- **Codex** speaks only the OpenAI Responses API, so the Connection needs an
  `openai_responses` endpoint.
- **Grok Build** speaks the OpenAI Chat protocol (`openai_chat`) with an API key.
  On a cloud machine it uses the CLI's own sign-in instead.
- **Antigravity** has no API-key protocol (`inference_protocols` is empty). It
  runs only on a cloud machine, under its own sign-in.
- **Claude Code** speaks only Anthropic Messages (`ANTHROPIC_BASE_URL`); no
  Claude subscription login is involved.
- **Command Code** runs in its documented local-only BYOK mode
  (`--local-only`): no Command Code account or backend traffic.

A disabled provider cannot be selected. Use `GET /api/harnesses` for the
installed catalog, `GET /api/models` for Connection-scoped model availability,
`GET /api/executor-backends` for executors and `GET /api/subscription-providers`
for the subscription providers this deployment offers. These reads have no side
effects.

A Harness switch creates a new linked Session with an explicit summary Message.
SBX never silently translates hidden context between CLIs.
