---
title: Security model
description: Understand the security boundaries and credential handling.
---

## Security boundary

**The Modal Sandbox is the only security boundary.** Everything running inside the sandbox is contained by the VM; no Modal token, no platform credential, and no control-plane secret can escape.

Provider CLIs run with their own sandboxing disabled (e.g., `--dangerously-bypass-approvals-and-sandbox` for Codex) because in-CLI sandboxing is unreliable under gVisor. Containment is provided by the VM, not the CLI.

## Credential handling

### Import

Provider credentials are imported as file blobs:

```bash
uv run python -m control.onboarding --modal import \
  --provider devin \
  --from ~/.local/share/devin/credentials.toml \
  --account-id devin-1
```

The credential blob is structured as:

```json
{
  "provider": "devin",
  "files": {
    ".local/share/devin/credentials.toml": "..."
  }
}
```

### Storage

Credentials are stored as Modal Secrets (encrypted at rest). The control plane **never logs or prints** credential material.

### Injection

At sandbox creation, the credential files are:
1. Restored to `$HOME` at permissions `0600` (read/write owner only)
2. **Stripped from the provider CLI's child environment** — the runner explicitly excludes credential-shaped env vars:
   - Codex: `shell_environment_policy.exclude`
   - Devin: also strips `ACP_BACKEND`, `DEVIN_*`, `WINDSURF_*`
   - Grok: strips `GROK_*`, `XAI_*`

### Write-back

OAuth providers (Devin) support automatic credential refresh. After each turn, the runner execs `export-credentials` in the sandbox. If the CLI rotated tokens, the refreshed blob is:
1. Committed to the account's credential record
2. Used to recreate the Modal Secret in place (no redeploy needed)

This is a **compare-and-swap operation**: if another process holds the lock, the update is skipped (no race condition).

## API authentication

### `/v1` (public REST API)

Bearer token `Authorization: Bearer sbx_<key>`:
- Plaintext shown once at creation
- Server stores `sha256(key)` only
- Scopes: `agents` (default) or `admin`

### `/api/*` (internal API, legacy)

HTTP Basic Auth (one shared deployment credential):
- User: `sbx`
- Password: `sbx` (not a real secret; only used locally)
- The web console uses `/v1`, not `/api`. `/api` is not a public surface; do not expose or build on it

## Artifact collection

Artifact creation **fails closed** on suspected secret material:

```http
HTTP/1.1 409 Conflict
Content-Type: application/json

{
  "error": {
    "code": "artifact_secret",
    "message": "forbidden content found in workspace files: config/settings.py"
  }
}
```

The control plane scans collected file **contents** for the agent's own credential values (the account's credential blob plus other sandbox secrets) and aborts the whole snapshot on a match — the check is a value scan, not a filename heuristic. Credential/key-material filenames (`.env*`, `*.pem`, `*.key`, `auth.json`, …) and git internals are refused outright. Remove sensitive files before creating an artifact.

## Rules

**You must follow these rules:**

1. **Never commit credentials to version control.** See `.gitignore`.
2. **Rotate leaked credentials immediately.**
   - New account + new Secret
   - Revoke old API keys (`DELETE /v1/api-keys/{id}`)
   - Revoke old accounts (`DELETE /v1/accounts/{id}`)
3. **Use fine-grained GitHub PATs or Apps.** Limit scope to agent repos and a short lifetime (30 days).
4. **Keep Modal tokens secure.** They grant access to your entire workspace; rotate if exposed.
5. **Test artifact uploads** with non-sensitive files first.

## Reporting vulnerabilities

**Do not open a public issue for security vulnerabilities.**

Use GitHub's private vulnerability reporting (Security tab → "Report a vulnerability") or contact maintainers directly.

Include: affected version, reproduction steps, impact. Never include real tokens or credentials; describe the file names instead.

## Audit & monitoring

The control plane does not provide audit logs (not yet). In the near term:

- Review Modal app logs: `modal app logs sbx-control --tail`
- Monitor Dict updates: `modal dict ls` (lists but not contents)
- Track agent creation via your client's logs

Consider:
- Storing API keys in a secret manager (not environment variables)
- Rotating API keys periodically
- Using a reverse proxy (edge Worker) to log requests to `/v1`
