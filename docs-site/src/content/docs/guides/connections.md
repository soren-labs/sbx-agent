---
title: Connections
description: Add, validate, replace and disconnect the Modal, GitHub, OpenCode Zen and Codex Connections.
---

Every external account is a **Connection** with encrypted, versioned
credentials. There are four kinds, all added by manual input:

| Kind | Fields | Used for | Validation probe |
| --- | --- | --- | --- |
| `modal` | `token_id`, `token_secret` | Creating and releasing sandboxes. Only the executor worker sees it; the sandbox gets a lease-scoped key. | `App.lookup` with the token |
| `github` | `token` | Cloning repositories and Delivery (push, pull request, merge). | `GET /user`, plus repository permission for Project repos |
| `opencode_zen` | `api_key` | Inference for the OpenCode Harness. Written to an isolated HOME and scrubbed after each Turn. | One minimal free-model chat request (this consumes quota and is recorded) |
| `codex` (optional) | `auth_json` | The experimental Codex Harness, in an isolated `CODEX_HOME`. | Format check only |

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

`GET /api/models` crosses the Zen model list with public pricing. Free models
are marked `usable_via: official_opencode_cli`, and the first free model in a
fixed preference order is the `preferred_model`, used when a Session sets none.

## CLI

```bash
printf '%s' "$TOKEN" | sbx connections add github --label work
sbx connections list
sbx connections validate CONNECTION_ID
sbx connections replace CONNECTION_ID --credential-file new.json
```
