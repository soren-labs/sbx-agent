---
title: Security model
description: Execution isolation, provider/GitHub credentials, API keys and artifact leak prevention.
---

## Trust boundary

A Modal Sandbox is the primary **execution isolation boundary** for an agent
run. Provider CLIs execute inside that sandbox; the control plane, Modal
workspace credentials and long-lived operator secrets stay outside it.

Some provider CLIs run with their own confirmation/sandbox layer relaxed
because SBX relies on the outer isolated VM for execution containment. Treat a
sandboxed coding agent as untrusted code with access only to the credentials
and resources explicitly injected for that task.

## Provider credentials

Normal connection uses the provider's official login flow:

```bash
./sbx auth login --provider devin
```

SBX captures the provider credential, stores it in the managed account
credential path and verifies it before scheduling. Credential contents are not
returned by account APIs or printed by normal status commands.

At sandbox creation, only the selected account's credential is restored into
the provider's expected home/config location. Provider-specific credential
environment variables are stripped from child-command environments where the
runtime supports that protection.

OAuth providers can refresh/rotate their own credential. SBX can capture that
write-back and update the managed account atomically.

Manual credential-file import exists for migration/advanced automation, but it
is not the default user workflow.

## GitHub credentials

The default GitHub integration is the pre-registered SBX GitHub App. The user
grants repository access once on GitHub; a trusted broker holds the App's
long-lived private key and mints **short-lived installation tokens** when the
self-hosted deployment needs repository access.

The shared App private key is not copied into the self-hosted control plane or
agent sandbox. Installation tokens are not written into repository config.

Deployment-owned Apps and PATs are advanced alternatives. If you choose them,
you own their long-lived secret storage/rotation.

## Public API keys

`/v1` uses:

```http
Authorization: Bearer sbx_...
```

- plaintext is shown only when the key is created;
- the running server stores the hash, not plaintext;
- normal task work uses the `agents` scope;
- account/API-key/GitHub administration requires `admin`.

The durable operator recovery credential is the bootstrap admin key created by
`sbx deploy` at `~/.local/state/sbx/bootstrap.key`, backed by the deployment
bootstrap Secret.

Current v0.1.1 limitation: keys minted through `/v1/api-keys` or the Console
are held in the running control plane and are cleared on restart/redeploy.
Use the bootstrap key to mint replacement scoped keys after an upgrade.

`sbx open` uses a one-time browser grant so the long-lived bootstrap key does
not have to appear in a Console URL.

## Secrets requested by tasks

Sandbox resource requests are allowlisted by the operator. Do not expose a
Modal Secret/MCP entry just because a task names it; configure only the
resources your deployment intentionally allows coding agents to receive.

## Artifact leak prevention

Artifact/workspace packaging fails closed when it detects known credential
values or credential/key-material files. This reduces accidental exfiltration
through durable artifacts but is not a substitute for keeping secrets out of
the repository/workspace in the first place.

If a credential enters git history, rotate it and remove it from history; do
not rely on artifact filtering to make the credential safe again.

## Operator rules

1. Keep Modal tokens and the bootstrap admin key in an operator secret store.
2. Connect only provider/GitHub accounts the deployment actually needs.
3. Grant the GitHub App only the repositories required by the product.
4. Prefer scoped runtime API keys for clients; rotate/re-mint them when needed.
5. Keep task resource Secret/MCP allowlists minimal.
6. Never put real tokens in prompts, screenshots, logs, issues or documentation examples.

## Vulnerability reporting

Do not open a public issue containing exploit details or credentials. Use the
repository's private GitHub vulnerability-reporting channel when available, or
contact the maintainers privately. Include affected version, reproduction and
impact with secret values redacted.
