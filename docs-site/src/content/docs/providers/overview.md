---
title: Provider Support
description: Support matrix, status, and per-provider notes.
---

## Support matrix (v0.1.1)

| Provider | Status | CLI | Auth | Multi-turn | Cancel | Multi-account | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Codex** | Stable | `@openai/codex` 0.153.0 | ChatGPT (`.codex/auth.json`) | ✅ | ✅ | ✅ | Real-account Modal E2E; authentication deferred (external token reauth needed) |
| **Devin** | Experimental | 3000.10.21 | `.local/share/devin/credentials.toml` | ✅ | ✅ | ✅ | Verified in production; two turns, cancel, zero credential leaks |
| **Antigravity** | Experimental | `agy` 1.2.3+ | `.gemini/antigravity-cli/antigravity-oauth-token` | ✅ | ✅ | ✅ | Real-account gate verified; 4×1-slot fleet, failover, no leaks |
| **Grok** | Experimental | `grok` 1.0.24+ | `.grok/auth.json` | ✅ | ✅ | ✅ | Real-account gate verified; auto distribution, failover, reaper |
| **OpenCode** | Experimental | `opencode-ai` 1.18.29 | `.local/share/opencode/auth.json` | ✅ | ✅ | ✅ | Real-account gate verified; two turns, cancel, zero leaks |
| **Claude** | Not supported | — | — | — | — | — | Experimental adapter merged but not registered (replay-only) |

## Status definitions

- **Stable** — production adapter + pinned CLI + real-account Modal E2E on release tag
- **Experimental** — production code merged + some real-account evidence, but not complete release-gate matrix
- **Preview** — production adapter/image/credentials merged, but no real-account evidence yet (fixture/replay only)
- **Not supported** — no working production path in this tag

## Evidence policy

Matrix rows cite only evidence that ran against **real accounts** on Modal. Credential gaps are recorded as deferred, not passes. The matrix is refreshed at every release.

- `v0.1.1` — no new provider gates; carries over `v0.1.0-alpha` evidence
- `v0.1.0-alpha` — all five providers (codex, devin, agy, grok, opencode) passed real-account gates; codex authentication is deferred (stale ChatGPT token, external, not a product failure)

## Per-provider notes

### Codex

**Stable.** Native events are canonical (pass-through). Requires `codex login` locally.

- **Resume:** `--resume <thread_id>`
- **Cancel:** SIGTERM the process
- **Multi-account:** Via separate CLI logins and credential imports
- **Known limitations:** Authentication deferred (external token reauth needed)

### Devin

**Experimental.** Driven over ACP (JSON-RPC) or direct CLI.

- **Resume:** Native session / ACP session
- **Cancel:** SIGTERM
- **Multi-account:** Import multiple credential files with different `--label` values; set per-account concurrency with `--slots`
- **Transport:** `SBX_DEVIN_TRANSPORT=acp` (default) or `cli`
- **MCP support:** ✅ Yes (only provider with MCP)
- **Env scrubbing:** `ACP_BACKEND`, `DEVIN_*`, `WINDSURF_*`
- **Evidence:** Production verified; two turns, cancel, usage, zero leaks

### Antigravity

**Experimental.** Agy CLI (Google Gemini agent).

- **Resume:** `--conversation <id>`
- **Cancel:** SIGTERM
- **Multi-account:** Import multiple accounts via `control.onboarding add --provider antigravity`
- **Quirk:** Stale conversation IDs silently fork a new conversation; adapter detects and fails
- **Evidence:** Fleet verified; 4×1-slot distribution, failover on auth_invalid, no leaks

### Grok

**Experimental.** XAI's reasoning agent.

- **Resume:** `--resume <id>`
- **Cancel:** SIGTERM
- **Multi-account:** Import multiple accounts via `control.onboarding add --provider grok`
- **Quirk:** No native `init` event; session ID is first stream marker
- **Env scrubbing:** `GROK_*`, `XAI_*`
- **Evidence:** Fleet verified; auto scheduling, exhaustion + failover, reaper reconciliation

### OpenCode

**Experimental** (promoted from Preview in v0.1.1). OpenCode CLI.

- **Resume:** `--session <id>`
- **Cancel:** SIGTERM
- **Multi-account:** Import multiple accounts via `control.onboarding add --provider opencode`
- **Evidence:** Production verified; two turns, cancel, usage, zero leaks

### Claude

**Not supported** (experimental adapter merged but not registered). Use `--experimental` flag for early testing only.

## Provider CLI versions

Pin versions in `runtime/packages.txt` or override at deploy time:

```bash
export SBX_DEVIN_VERSION="3000.10.21"
export SBX_CODEX_VERSION="latest"  # resolves npm dist-tag once
uv run sbx deploy
```

The build host resolves `latest` once and freezes the version into `cli-versions.json`. Sandboxes never install `@latest` dynamically.

## Credential import

Each provider has a login command and credential file:

| Provider | Login | Credential file |
| --- | --- | --- |
| Codex | `codex login` | `~/.codex/auth.json` (via Modal Secret `sbx-codex-auth`) |
| Devin | `devin` | `~/.local/share/devin/credentials.toml` |
| Antigravity | `agy` (OAuth) | `~/.gemini/antigravity-cli/antigravity-oauth-token` |
| Grok | `grok login` | `~/.grok/auth.json` |
| OpenCode | `opencode auth login` | `~/.local/share/opencode/auth.json` |

Import via CLI:

```bash
uv run python -m control.onboarding --modal add \
  --provider devin \
  --from ~/.local/share/devin/credentials.toml \
  --label "devin-prod"
```

`sbx deploy` also seeds one account per selected provider, and
`SBX_<PROVIDER>_ACCOUNTS` (a JSON list of `{"id", "slots"?, …}`) seeds a
whole pool at deploy time. See [Accounts & credentials](/guides/accounts/)
for both paths, slots and failover.
