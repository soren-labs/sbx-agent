---
title: Security
description: The vault, credential boundaries, no ambient credentials, redaction, and the access model.
---

## Identity

- Passwords use argon2id; unknown accounts cost the same work as known ones.
- Email verification and reset tokens are single use and stored only as hashes.
  Verification expires after 24 hours.
- Cookie sessions are `HttpOnly`, `SameSite=Lax` and `Secure` when configured.
  Cookie mutations need a CSRF token and an allowed `Origin`.
- API keys (`sbx_key_` prefix) are stored hashed; plaintext is shown once.
- Changing or resetting a password bumps `auth_epoch` and revokes cookie
  sessions.
- Cross-workspace access is reported as `not_found`; holding an ID grants
  nothing.

## Vault

CredentialVersions are encrypted with AES-256-GCM. The associated data binds
the workspace, Connection, version and format, so ciphertext cannot be moved
between records. The keyring (`SBX_VAULT_KEYS`) lives outside the database.
Replacing or disconnecting a Connection bumps a revocation epoch that
invalidates grants, and the broker refuses to decrypt revoked material.

## No ambient credentials

SBX never reads provider or cloud credentials from the host environment, host
files or other users. A Session uses only Connections in its own workspace.
The runtime builds the CLI's environment from an explicit allowlist rather than
inheriting `os.environ`.

| Secret | Where plaintext goes |
| --- | --- |
| Modal token | the executor worker only |
| GitHub token | the delivery worker, and a clone helper that reads it from a private file (never a URL or argv) |
| OpenCode Zen key | the CLI's isolated HOME, scrubbed after each Turn |
| Codex `auth.json` | an isolated `CODEX_HOME`, scrubbed after each Turn |

ChangeSets and checkpoints exclude credential files, including `.env`,
`auth.json` and private keys. Safe templates such as `.env.example` remain
ordinary files. Checkpoints also exclude files containing selected credentials
or obvious secret patterns; selected credentials in Git metadata refuse the
checkpoint. Restore filters credential files and secret content from older
archives, and rejects traversal or link escapes. Native state is checked for
selected credential values so ordinary transcript examples remain usable.

## Redaction

Known secrets and secret-looking structured fields are redacted before runtime
evidence is spooled, and before public errors are emitted. Request validation
errors never echo input. File reads refuse credential paths with `403 forbidden`
and redact selected credentials and secret patterns in ordinary files. ChangeSet
capture refuses selected credentials and secret patterns. Worker tracebacks,
runtime output and stored fault messages are scrubbed before exposure. Audit
records store the target, version, purpose, actor and result, never secret material.

## Runtime channel

The control plane talks to each runtime with HMAC-signed grants derived from
`SBX_RUNTIME_MASTER_KEY`, scoped to one lease and generation with a 5-minute
TTL. A stale generation is rejected (`stale_fence`). The runtime offers no
generic exec; mutating operations are a fixed set.

## Limits to know

Project code and the official CLI can read any credential explicitly placed in
their environment, so SBX does not claim tamper-proof attestation. Provider-side
token revocation on disconnect is not performed. Preview origins and OAuth
sign-in are not implemented.
