# Security policy

## Reporting a vulnerability

**Do not open a public issue for security reports.**

- Preferred: use GitHub's private vulnerability reporting ("Report a
  vulnerability" on the repository's Security tab).
- If the repository does not have private reporting enabled, contact the
  maintainers through the channel listed in the repository profile.

Include: affected version/commit, reproduction steps, impact, and whether
credential material is involved. **Never include real tokens, credential
files, or credential blobs in a report** — describe the file and field names
instead; we will arrange a secure channel if material is needed.

We aim to acknowledge reports within a few days. There is no bug bounty.

## Supported versions

| Version | Supported |
| --- | --- |
| unified architecture (`main`, latest tag `v0.1.2`) | ✅ |
| `v0.1.1` and earlier pre-unification releases | ❌ superseded |
| anything older | ❌ upgrade |

This is pre-1.0 software; only the current `main` receives fixes.

## Security model — what to know before deploying

- **The sandbox is the security boundary**, not the provider CLI's own
  sandboxing. Official CLIs run with in-CLI approvals bypassed *inside* the
  Executor sandbox (Modal or local); the sandbox holds no control-plane
  database access, vault keys or other users' credentials.
- **Credentials are encrypted CredentialVersions.** Connection secrets are
  envelope-encrypted with `SBX_VAULT_KEYS`, never returned by `/api`, and
  delivered to `sbx-runtime` only as lease-scoped grants for the Turn that
  needs them. API keys are stored as hashes; plaintext is shown once.
- **One authenticated surface, `/api`.** Browser sessions use HttpOnly cookies
  plus `X-CSRF-Token` on mutations; programmatic access uses API keys. Every
  resource is owner/workspace scoped and other owners' resources return
  `404 not_found`.
- **Shipping is gated.** Deliveries push only the exact immutable ChangeSet
  subject; merges re-check the exact-subject gate (fresh review/check
  evidence, remote head) before acting.

## Rules that protect you (and this project)

- Never commit `.env`, `auth.json`, `.modal.toml`, credential files, or any
  token — see `.gitignore`.
- Fixtures, tests, logs, PRs and issues must use `REDACTED` placeholders, and
  e2e tooling records only sha256-16 fingerprints.
- Rotate any credential that may have leaked: replace the Connection's
  credential (`sbx connections replace`, or **Settings → Connections**) or
  disconnect it, and revoke exposed API keys in **Settings → API keys**.
