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
| `0.1.x` (latest alpha tag) | ✅ fixes land on the release branch |
| anything older | ❌ upgrade |

This is a public alpha; only the newest release tag receives fixes.

## Security model — what to know before deploying

- **The Modal Sandbox is the security boundary**, not the provider CLI's own
  sandboxing. Provider CLIs run with approvals/sandbox bypassed *inside* the
  VM (e.g. codex's `--dangerously-bypass-approvals-and-sandbox`) because
  in-CLI sandboxing is unreliable under gVisor. Anything an agent does is
  contained by the sandbox VM, which holds **no Modal token and no platform
  credentials**.
- **Your credentials stay in your workspace.** Provider credential files are
  imported into your Modal workspace (Secrets / Dict blobs), injected into
  sandboxes at `0600`, and stripped from the CLI child environment. API keys
  (`sbx_<key>`) are stored server-side as `sha256` only; plaintext is shown
  once at creation.
- **`/v1` is the public surface** (Bearer). `/api/*` is HTTP Basic and exists
  for the bundled dashboard — do not expose it as a public API.
- **Artifact collection fails closed**: suspected secret material in a
  workspace snapshot aborts with `409 artifact_secret`; nothing is persisted.

## Rules that protect you (and this project)

- Never commit `.env`, `auth.json`, `.modal.toml`, credential files, or any
  token — see `.gitignore`.
- Fixtures, tests, logs, PRs and issues must use `REDACTED` placeholders, and
  e2e tooling records only sha256-16 fingerprints.
- Rotate any credential that may have leaked — `POST /v1/accounts/{id}/verify`
  re-probes an account, `DELETE /v1/accounts/{id}` removes it, and
  `DELETE /v1/api-keys/{id}` revokes an API key.
