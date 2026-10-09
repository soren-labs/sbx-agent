---
title: Quick start
description: Self-host SBX with PostgreSQL, sign in to the Console, add four Connections and run a first Turn.
---

This path runs everything on your machine except the sandbox, which runs on
Modal.

## Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/), and Node 22+ for the Console.
- A reachable PostgreSQL database.
- The repository checked out (`uv sync`).

## Minimum accounts and credentials

| Needed | Why |
| --- | --- |
| Email and password account | Product sign-in. Self-hosted verification mail lands in a local directory unless you configure Resend. |
| Modal token (`token_id`, `token_secret`) | Creates and tears down sandboxes. |
| GitHub token | Clones repositories and delivers pull requests. |
| Inference API key | Your own model provider: API key, base URL and model (for example DeepSeek, or any OpenAI- or Anthropic-compatible endpoint). One key serves every coding CLI that speaks its protocol; a second key is optional. |

## 1. Configure and start the control plane

```bash
export SBX_DATABASE_URL=postgresql://sbx:YOUR_PASSWORD@127.0.0.1:5432/sbx
export SBX_VAULT_KEYS="k1:$(openssl rand -base64 32)"
export SBX_RUNTIME_MASTER_KEY="$(openssl rand -hex 32)"
export SBX_DATA_DIR=./.sbx-data
# Console dev server origin (used in verification links and CSRF checks)
export SBX_PUBLIC_URL=http://localhost:5174

uv run sbx migrate   # optional: `serve` also migrates on start
uv run sbx serve --port 8800
```

`serve` applies migrations, starts the in-process workers and listens on
`127.0.0.1:8800`. Check `curl http://127.0.0.1:8800/readyz`. See
[Configuration](/self-hosting/configuration/) for every variable.

## 2. Start the Console

`sbx serve` serves the API only. Run the Console from its own directory; it
proxies `/api` to the control plane.

```bash
cd console
npm ci
SBX_API_PROXY_TARGET=http://127.0.0.1:8800 npm run dev   # http://localhost:5174
```

## 3. Create and verify your account

Register at `/register`. Registration always answers `202`, whether or not the
email exists. Without `SBX_RESEND_API_KEY` the verification email is written as
a JSON file under `$SBX_DATA_DIR/mail/`; open the link it contains
(`/verify-email?token=VERIFICATION_TOKEN`, valid 24 hours), then sign in.

## 4. Add Connections

The Console's setup checklist (home page) lists what is missing. Open
**Connections** and add, in any order: an inference API key, Modal and GitHub.
OpenCode, Codex, Claude Code, Grok Build and Command Code all run on that key;
nothing vendor-specific is required. Secret fields are write-only; after submit SBX validates each
Connection in the background and shows its health.

From the CLI the same inputs are read from stdin or a file, never argv:

```bash
uv run sbx auth login --email you@example.com          # password via stdin or prompt
printf '%s' "$GITHUB_TOKEN" | uv run sbx connections add github
echo '{"token_id":"MODAL_TOKEN_ID","token_secret":"MODAL_TOKEN_SECRET"}' | uv run sbx connections add modal
```

## 5. Run a first Turn

Start a new Session from the Console home page (the composer defaults to the
OpenCode Harness, the preferred free model and Modal), or from the CLI:

```bash
uv run sbx execute "Add a CONTRIBUTING.md with one paragraph."
```

Follow the Turn in the Conversation and Activity tabs. When it succeeds in a
Session with a repository, SBX captures a ChangeSet automatically; review it
and request a Delivery from the Changes tab. A scripted version of the same flow
is `examples/unified_mvp.py`; see the [Python SDK](/sdk/python/).

## Troubleshooting

See [Troubleshooting](/troubleshooting/) for the most common first-run
failures.
