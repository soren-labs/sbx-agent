---
title: Connections
description: Add, validate, replace and disconnect inference API key, Modal and GitHub Connections.
---

Every external account is a **Connection** with encrypted, versioned
credentials. There are three kinds, all added by manual input:

| Kind | Fields | Used for | Validation probe |
| --- | --- | --- | --- |
| `modal` | `token_id`, `token_secret` | Creating and releasing sandboxes. Only the executor worker sees it; the sandbox gets a lease-scoped key. | `App.lookup` with the token |
| `github` | `token` (personal access token or GitHub App installation token) | Cloning repositories and Delivery (push, pull request, merge). | Repository access for Project repos (`GET /repos/…`); `GET /user` only supplies the identity, so installation tokens are accepted |
| `inference_api` | `api_key`, `model`, `endpoints` | Inference for every Harness, with your own provider key. Passed to the CLI as an environment variable; never written to disk. | One minimal generation request per endpoint (this consumes a little quota and is recorded) |

## Modal sandboxes

Every Modal sandbox SBX creates is a full Linux virtual machine in your own Modal
workspace, whatever inference you use. When a Modal Connection is verified, SBX
builds the shared runtime image there once (official CLIs, Git, Node and the
runtime), so the first Turn does not wait for an image build. Image builds and
sandbox time are billed to your workspace; sandboxes are terminated when the
Session's executor is released.

## Inference API keys

Inference is bring-your-own-key. A Connection is not a vendor integration: it is
an API key, a default model, and a base URL for each protocol the provider
speaks. DeepSeek is a built-in preset in the Console; any compatible provider or
gateway works the same way.

| Protocol | Request path | Typical base URL | Harnesses that can use it |
| --- | --- | --- | --- |
| `openai_chat` | `{base_url}/chat/completions` | `https://api.deepseek.com` | OpenCode, Grok Build, Command Code |
| `openai_responses` | `{base_url}/responses` | `https://api.deepseek.com` | Codex, OpenCode, Command Code |
| `anthropic_messages` | `{base_url}/v1/messages` | `https://api.deepseek.com/anthropic` | Claude Code, OpenCode, Command Code |

Fill in every protocol your provider offers so the same key can drive any CLI.
A Session is refused with `connection_required` when no Connection offers a
protocol its Harness accepts.

```python
conn = client.connections.add_inference(
    api_key,
    model="deepseek-flash",
    endpoints={
        "openai_chat": "https://api.deepseek.com",
        "openai_responses": "https://api.deepseek.com",
        "anthropic_messages": "https://api.deepseek.com/anthropic",
    },
    label="DeepSeek",
)
print(client.connections.wait_health(conn["id"])["health"])
```

Base URLs must be public `https` addresses; private, loopback and redirecting
targets are refused. Self-hosters with a gateway on a private network can set
`SBX_INFERENCE_ALLOW_PRIVATE_URLS=1`.

Validation failures set `health_reason`: `inference_rejected_key` (the provider
refused the key) or `inference_endpoint_or_model_rejected` (the base URL does not
match the protocol, or the model id is unknown).

OpenCode Zen keys and uploaded Codex `auth.json` credentials from earlier
versions can no longer be added. Existing ones are kept, listed as retired, and
keep serving the Sessions that already use them.

## Secrets are write-only

Responses never contain plaintext, ciphertext or fingerprints. They expose the
credential version id, ordinal and format. Validation errors never echo your
input. The Console clears secret fields after submit and never stores them in
web storage.

## Health

Configuration state is `configured`, `disabled` or `revoked`. Observed health is
separate: `unverified`, `verifying`, `ready`, `degraded` or `reauth_required`.
Validation runs as a background Job against the exact credential version, so a
freshly added Connection starts `unverified` and settles shortly after.

```python
conn = client.connections.add("github", {"token": token})
view = client.connections.wait_health(conn["id"])
print(view["health"])
```

## Replace and disconnect

- **Replace** appends a new CredentialVersion with a compare-and-set on the
  Connection version (`expected_version`), invalidates old health and
  capability observations, and queues validation of the new version. A
  validation result that no longer matches the current version is discarded.
- **Disconnect** is refused with `409 connection_in_use` while live or
  quarantined leases or live Executions depend on it. Once released it revokes
  the Connection, cancels queued Jobs that target it and makes the vault refuse
  to decrypt it. Revoking the token at the provider is separate and reported
  as `not_performed`.

## Selection

A Session only considers Connections in its own workspace: those named
explicitly, or the configured ones chosen by priority (skipping
`reauth_required`). Environment variables, host files and other users'
credentials are never consulted. A missing one surfaces as
`connection_required` or `credential_invalid` on the Turn.

## Models

`GET /api/models?provider_id=HARNESS` lists, per inference Connection, its
models, the `protocol` that Harness would use and whether it is `compatible`.
The Connection's default `model` is used when a Session sets none; any model id
you pass explicitly is pinned as given.

## CLI

```bash
printf '%s' "$TOKEN" | sbx connections add github --label work
printf '%s' "$KEY" | sbx connections add inference_api --label DeepSeek \
  --model deepseek-flash --endpoint openai_chat=https://api.deepseek.com
sbx connections list
sbx connections validate CONNECTION_ID
sbx connections replace CONNECTION_ID --credential-file new.json
```
