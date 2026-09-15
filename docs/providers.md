# Provider support

sbx-browser drives **official provider CLIs** inside sandboxes — it never
talks to model APIs directly and never converts subscription quota into an
API. Each provider is driven through an `AgentAdapter`
(`runtime/runner/adapter.py`): argv construction, credential-file
declaration, native-event → canonical-event translation, native session-id
extraction, and health classification.

## Support matrix (v0.1.0-alpha)

| Provider | Status | CLI / version | Auth material (imported file, rel. `$HOME`) | Multi-turn | Cancel | Multi-account | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| codex | **Stable** | `@openai/codex` **0.153.0** — pinned in `sbx-runtime` (`runtime/packages.txt`) | `.codex/auth.json` (ChatGPT `codex login`) | ✅ `codex exec resume` | ✅ | ✅ `SBX_CODEX_ACCOUNTS` | `tests/e2e_modal/` real-Modal suite (two-turn, concurrency, no-leak); committed `timings.json`; P0 spike |
| devin | Experimental | Devin CLI **3000.10.21** — sha256-pinned bundle in `sbx-runtime-devin` | `.local/share/devin/credentials.toml` | ✅ ACP session | ✅ | ✅ `SBX_DEVIN_ACCOUNTS` / burst slots | SOR-73 spike: Modal clean-room credential injection + `auth status` + `-p` smoke, 2/4/8-way concurrency PASS — 2026-09-14 (`spike/p2/`); full `/v1` e2e gate pending |
| antigravity | Experimental | your own `agy` binary (`SBX_AGY_BIN` / `~/.local/bin/agy`, baked into `sbx-runtime-antigravity`) | `.gemini/antigravity-cli/antigravity-oauth-token` | ✅ `--conversation <id>` | ✅ | ✅ `SBX_ANTIGRAVITY_ACCOUNTS` | Real-account gate harness merged: `tests/e2e_modal/agy_gate.py` (init → 2 turns → stale-resume → export → leak scan); SOR-68 multi-account fleet matrix pending |
| grok | Experimental | your own `grok` binary (`SBX_GROK_BIN` / `~/.local/bin/grok`, verified against real 1.0.24 stream shape) | `.grok/auth.json` | ✅ `--resume <id>` | ✅ | ✅ `SBX_GROK_ACCOUNTS` | Real-account gate harness merged: `tests/e2e_modal/grok_gate.py`; SOR-68 fleet matrix pending |
| opencode | **Not supported at this tag** | — | `.local/share/opencode/auth.json` (planned) | — | — | — | Provider id reserved in the contract; no production adapter registered, scheduling refuses (`provider_exhausted`). Being built under SOR-96 |
| claude | **Not supported** | — | — | — | — | — | No provider id or adapter in this release |

### Evidence policy

- **Stable** = production adapter + pinned runtime **and** a passing
  real-account Modal E2E on the release tag. Fakes, replays and local-only
  runs never count as Stable evidence.
- **Experimental** = the production code path is merged and *some*
  real-account evidence exists (spike or gate harness), but the complete
  release-gate matrix for that provider has not finished.
- **Not supported** = no working production path in this tag, regardless of
  reserved ids or in-flight work.
- A row may only cite evidence that actually ran against a real account;
  credential gaps are recorded as deferred, never as passes. The matrix is
  refreshed at every release.

## How accounts & credentials work

```
local official CLI login                your Modal workspace
 ~/.codex/auth.json ─┐                  sbx-accounts Dict
 ~/.grok/auth.json  ─┼── import ──────▶  account/<id>     (record)
 credentials.toml  ─┤                   credential/<id>  (blob)
 oauth-token       ──┘                        │
                                             │ mount as Secret sbx-acct-<id>
                                             ▼
                              sandbox $HOME (files restored @0600)
                                             │ runner export-credentials
                                             └──────── write-back if refreshed
```

- One provider can hold **multiple accounts**; `account_id: "auto"` picks a
  free one (LRU + per-account `max_concurrent` slots + cooldown on
  `auth_invalid`/`rate_limited`). Name an account explicitly to pin it.
- `POST /v1/accounts/{id}/verify` probes a credential in a throwaway sandbox
  without spending a session.
- **Never** paste credential material into issues, PRs, logs, fixtures, or
  Linear — fixtures use `REDACTED` placeholders; the e2e gates record
  sha256-16 fingerprints only.

## Importing credentials

```bash
# bootstrap CLI (SOR-98)
sbx accounts import --provider codex --from ~/.codex/auth.json

# offline admin CLI (file store locally; --modal for the workspace Dict)
python -m control.accounts --modal import \
  --provider antigravity --from ~/.gemini/antigravity-cli/ --slots 4

# /v1 API (admin-scoped key; credential never echoed back)
curl -X POST $SBX_BASE_URL/v1/accounts \
  -H "Authorization: Bearer $SBX_API_KEY" -H 'content-type: application/json' \
  -d '{"provider":"grok","label":"work",
       "credential":{"files":{".grok/auth.json":"<file contents>"}},
       "max_concurrent":2}'
```

Credential files per provider (the adapter's declared `credential_files` —
directory imports take the containing dir):

| Provider | Files |
| --- | --- |
| codex | `.codex/auth.json` |
| devin | `.local/share/devin/credentials.toml` |
| antigravity | `.gemini/antigravity-cli/antigravity-oauth-token` |
| grok | `.grok/auth.json` |

## Provider-specific notes

**codex.** The reference provider: native events are already canonical and
pass through. `--dangerously-bypass-approvals-and-sandbox` is used because
the sandbox (not Codex's Landlock/seccomp layer) is the security boundary —
see [architecture.md](architecture.md). `CODEX_AUTH_JSON` remains a supported
v1-style credential path for codex only.

**devin.** Driven over the official ACP stdio protocol (`devin acp`,
JSON-RPC) via `runtime/runner/adapters/devin_acp.py`; `SBX_DEVIN_TRANSPORT=cli`
switches to direct `devin -p`. The runner strips `ACP_BACKEND`,
`DEVIN_API_KEY`/`DEVIN_V3_API_KEY`/`DEVIN_LEGACY_API_KEY`/`DEVIN_ORG_ID` and
`WINDSURF_*` from the child env — `ACP_BACKEND=windsurf` makes a valid
`credentials.toml` report "Not logged in" (verified on host and in Modal).

**antigravity.** Stale `--conversation` ids make the real CLI exit 0 while
silently opening a *new* conversation; the adapter records the requested id
and fails the turn (runner exit 2) rather than forking the session.

**grok.** Real `streaming-json` has no `init` line — `end.sessionId` is the
first session marker. Step-level `usage.signature` is redacted before
persistence. Stale `--resume` exits rc=1 with a stderr 404 signature and no
stream events, so the rc path fails the turn.

**opencode / claude.** Not supported in `v0.1.0-alpha` — see the matrix.
