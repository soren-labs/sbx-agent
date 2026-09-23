---
title: Provider Details
description: Per-provider configuration, quirks, and model support.
---

## Codex

**Status:** Stable

**Install:** `npm install -g @openai/codex@0.153.0`

**Login:** `codex login` (opens browser for ChatGPT auth)

**CLI:** `codex exec [--resume <thread_id>] [--json] <prompt>`

**Config:**
- Shared credential Secret: `sbx-codex-auth`
- Multi-account: Import via `control.onboarding --modal add --provider codex`
- Models: `gpt-5-turbo`, `gpt-5`, `gpt-4o`, etc.

**Known issues:**
- RC gate is credential-deferred (stale ChatGPT token; needs interactive reauth)
- Codex's built-in sandboxing is disabled (VM is the boundary)

## Devin

**Status:** Experimental

**Install:** Devin CLI 3000.10.21 (pinned in `sbx-runtime-devin`)

**Login:** `devin` (interactive terminal login)

**Transport:**
- Default: ACP (JSON-RPC over stdio)
- Alternative: `export SBX_DEVIN_TRANSPORT=cli` for direct CLI

**CLI:**
- ACP: `devin acp` (JSON-RPC protocol)
- Direct: `devin -p <prompt>`

**Config:**
- Account import: `control.onboarding --modal add --provider devin --from ~/.local/share/devin/credentials.toml --slots 4`
- Multi-account: Import separate credential files with different labels and slot counts
- Seeded account slots: the account `sbx deploy` seeds for Devin gets `SBX_DEVIN_BURST_SLOTS` slots (default 8); `SBX_DEVIN_SLOTS` overrides it

**MCP support:** ✅ Devin is the only provider with MCP (Model Context Protocol)

```json
{
  "resources": {
    "mcp": [
      {"name": "filesystem", "args": "${env:MCP_FILESYSTEM_ROOT}"}
    ]
  }
}
```

**Env scrubbing:** `ACP_BACKEND`, `DEVIN_*`, `WINDSURF_*` (prevents accidental credential leaks)

**Models:** `gpt-5-turbo`, `o1`, etc. (check `GET /v1/models`)

## Antigravity

**Status:** Experimental

**Install:** `agy` binary (Google Gemini-based agent)

**Login:** `agy` (OAuth browser login)

**Credential:** `~/.gemini/antigravity-cli/antigravity-oauth-token`

**CLI:** `agy init <task> --conversation <id> --json`

**Config:**
- Multi-account: `control.onboarding --modal add --provider antigravity --from <credential-path> --label <label>`
- Binary path: `SBX_AGY_BIN` (default `~/.local/bin/agy` or in `$PATH`)

**Resume:** `--conversation <id>` (must be exact; stale IDs fork a new conversation, which adapter detects and fails)

**Models:** `gemini-2.0-flash`, etc. (check `GET /v1/models`)

## Grok

**Status:** Experimental

**Install:** `grok` binary (XAI reasoning agent)

**Login:** `grok login` (stores to `~/.grok/auth.json`)

**Credential:** `~/.grok/auth.json`

**CLI:** `grok -p <prompt> --output-format streaming-json --resume <id>`

**Config:**
- Multi-account: `control.onboarding --modal add --provider grok --from ~/.grok/auth.json --label <label>`
- Binary path: `SBX_GROK_BIN` (default `~/.local/bin/grok` or in `$PATH`)

**Quirks:**
- No native `init` event; session ID is first stream marker
- Step-level usage signature is redacted before persistence
- Stale `--resume` exits rc=1 with stderr 404 signature (no stream events)

**Env scrubbing:** `GROK_*`, `XAI_*`

**Models:** `grok-3`, `grok-3-vision`, etc.

## OpenCode

**Status:** Experimental (promoted from Preview in v0.1.1)

**Install:** `opencode-ai` 1.18.29 (npm-pinned)

```bash
npm install -g opencode-ai@1.18.29
```

**Login:** `opencode auth login` (stores to `~/.local/share/opencode/auth.json`)

**CLI:** `opencode run <prompt> --format json --session <id>`

**Config:**
- Multi-account: `control.onboarding --modal add --provider opencode --from ~/.local/share/opencode/auth.json --label <label>`
- Models: `SBX_OPENCODE_MODELS` (default list, overridable)

**Models:** OpenCode Zen (preferred), OpenAI (fallback)

**Evidence:** Release 0.1 gate PASS (two turns, cancel, usage, zero leaks)

## Claude

**Status:** Not supported (experimental adapter merged but not registered)

**Adapter location:** `runtime/runner/adapters/claude.py` (read-only, not registered)

**Note:** Claude adapter is in replay-only mode; not usable for real agents yet.

## Model selection

Check available models:

```bash
curl -X GET "$SBX_BASE_URL/v1/models" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

Response:

```json
{
  "models": [
    {
      "id": "gpt-5-turbo",
      "provider": "codex",
      "name": "GPT-5 Turbo",
      "context_window": 100000,
      "supports_reasoning": true,
      "supports_mcp": false
    }
  ]
}
```

**Use `model` field in create-agent requests:**

```json
{
  "agent": {
    "provider": "devin",
    "model": "gpt-5-turbo"
  }
}
```

Unsupported models return `400 invalid_provider`.
