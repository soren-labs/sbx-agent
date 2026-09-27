---
title: AI providers
description: Connect provider logins and let SBX schedule verified accounts.
---

SBX runs **official provider CLIs** under subscriptions you already own. A
provider is usable only when its runtime is available and at least one account
has passed the cloud verification probe.

## Supported providers

| Provider | Public status | Normal login path | Multi-turn | Multi-account |
| --- | --- | --- | --- | --- |
| Codex | Supported | `./sbx auth login --provider codex` | Yes | Yes |
| Devin | Supported | `./sbx auth login --provider devin` | Yes | Yes |
| Antigravity | Supported | `./sbx auth login --provider antigravity` | Yes | Yes |
| Grok | Supported | `./sbx auth login --provider grok` | Yes | Yes |
| OpenCode | Supported | `./sbx auth login --provider opencode` | Yes | Yes |
| Claude | Experimental/advanced | explicit experimental path only | Adapter-dependent | Adapter-dependent |

Exact installed CLI versions are frozen per deployment; inspect them with
`./sbx status` rather than copying version numbers from documentation.

The exact provider support tier, runtime distribution and built-in model defaults for this docs build are generated from `runtime.provider_runtime` at [`/provider-reference.json`](/provider-reference.json).

## Connect an account

```bash
./sbx auth login --provider devin
./sbx auth status
```

`auth login` runs the provider's own login/OAuth flow, captures the credential
it writes, stores it in the deployment, then runs a cloud verification probe.
Only a verified account becomes schedulable.

To capture an existing provider login without signing in again:

```bash
./sbx auth import-existing --provider devin --from <credential-path>
```

Manual credential paths are an advanced migration tool, not the normal user
flow.

## Account selection

Tasks default to automatic scheduling. SBX selects an eligible verified
account with an available slot, taking cooldown/failover into account. Normal
task callers do not need to know an account id.

Pin `execution.provider`, `execution.account_id`, `model` or reasoning options
only when reproducibility or policy requires it. The [Task guide](/guides/tasks/)
shows the public request shape.

## Reconnect and verify

Use the same lifecycle from the console or CLI:

```bash
./sbx auth status
./sbx auth verify --provider devin
./sbx auth relink --provider devin
./sbx auth logout --provider devin
```

Run `./sbx auth --help` / `./sbx auth <command> --help` for the exact flags
supported by your installed version.

## Provider-specific behavior

Providers differ in their native session/resume protocol, but those details
are normalized behind Task/Run. Keep provider-specific troubleshooting in
[Provider notes](/integrations/provider-notes/) rather than encoding it into
normal task payloads.
