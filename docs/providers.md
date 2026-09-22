# Provider support

sbx-browser drives **official provider CLIs** inside sandboxes — it never
talks to model APIs directly and never converts subscription quota into an
API. Each provider is driven through an `AgentAdapter`
(`runtime/runner/adapter.py`): argv construction, credential-file
declaration, native-event → canonical-event translation, native session-id
extraction, and health classification.

## Support matrix (v0.1.1)

No provider pins, adapters or credential paths changed in `v0.1.1`; the
matrix below carries over the `v0.1.0-alpha` RC-plane evidence — no new
real-account gates were run for this tag.

| Provider | Status | CLI / version | Auth material (imported file, rel. `$HOME`) | Multi-turn | Cancel | Multi-account | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| codex | **Stable** | `@openai/codex` **0.153.0** — pinned in `sbx-runtime` (`runtime/packages.txt`) | `.codex/auth.json` (ChatGPT `codex login`) | ✅ `codex exec resume` | ✅ | ✅ `SBX_CODEX_ACCOUNTS` | `tests/e2e_modal/` real-Modal suite (two-turn, concurrency, no-leak); committed `timings.json`; P0 spike; RC gate lane **CREDENTIAL_DEFERRED** at `v0.1.0-alpha` and still deferred — stale ChatGPT token, needs interactive `codex login` (external, not a product failure; `docs/reviews/release-0.1-gate-core.md`) |
| devin | Experimental | Devin CLI **3000.10.21** — sha256-pinned bundle in `sbx-runtime-devin` | `.local/share/devin/credentials.toml` | ✅ ACP session | ✅ | ✅ `SBX_DEVIN_ACCOUNTS` / burst slots | SOR-73 spike: Modal clean-room credential injection + `auth status` + `-p` smoke, 2/4/8-way concurrency PASS — 2026-09-14 (`spike/p2/`); Release 0.1 `/v1` real-Modal gate **PASS** on the RC plane — two turns on one native thread, cancel, honest usage, zero leaks (`docs/reviews/release-0.1-gate-core.md`); exact-head reviewer leg of the cross-provider workflow gate **PASS** (`docs/reviews/SOR-107-gate-workflow.md`) |
| antigravity | Experimental | your own `agy` binary (`SBX_AGY_BIN` / `~/.local/bin/agy`, baked into `sbx-runtime-antigravity`; pin `agy_version` **1.2.3**, stream shape re-verified on real 1.2.3 — SOR-106) | `.gemini/antigravity-cli/antigravity-oauth-token` | ✅ `--conversation <id>` | ✅ | ✅ `SBX_ANTIGRAVITY_ACCOUNTS` | Real-account gate harness merged: `tests/e2e_modal/agy_gate.py` (init → 2 turns → stale-resume → export → leak scan); SOR-68 fleet gate **PASS** 50/50 on the RC plane — 4×1-slot fleet, `account_id=auto` distribution, cooldown + real `auth_invalid` failover, restart slot safety (`docs/reviews/release-0.1-gate-agy.md`) |
| grok | Experimental | your own `grok` binary (`SBX_GROK_BIN` / `~/.local/bin/grok`, verified against real 1.0.24 stream shape) | `.grok/auth.json` | ✅ `--resume <id>` | ✅ | ✅ `SBX_GROK_ACCOUNTS` | Real-account gate harness merged: `tests/e2e_modal/grok_gate.py`; SOR-68 runner lanes (`grok-1`/`grok-2`) + fleet gate **PASS** on the RC plane — auto distribution, exhaustion, cooldown failover, stranded-`running` reaper (`docs/reviews/release-0.1-gate-grok.md`) |
| opencode | Experimental | `opencode-ai` **1.18.29** — npm-pinned in `sbx-runtime-opencode` (`runtime/packages.txt` `opencode_*`) | `.local/share/opencode/auth.json` | ✅ `--session <id>` | ✅ | ✅ `SBX_OPENCODE_ACCOUNTS` | Real-account gate **PASS** on the RC plane — two turns on one native session, cancel, usage, zero leaks (`docs/reviews/release-0.1-gate-core.md`) |
| claude | **Not supported** | — | — | — | — | — | Experimental adapter seam merged but **not registered** in the provider registry (SOR-97, replay-only); not schedulable |

### CLI versions (SOR-175)

The `CLI / version` column is a **pin in `runtime/packages.txt`**. Any pin
(or its `SBX_<PROVIDER>_VERSION` env override) may be the literal `latest`:
it resolves **once** on the build host at `sbx deploy` / image build
(`runtime/versions.py`) — npm `latest` dist-tag for codex/opencode, the
promoted `{devin_base_url}/current/manifest.json` (with its per-platform
sha256 checksums) for devin, the host binary's `--version` for agy/grok —
and freezes into that deployment's `cli-versions.json` lock. Sandboxes only
ever see the frozen version: no floating `@latest` install ever runs at
sandbox start. Replay a previous lock via `sbx deploy --versions-lock` /
`SBX_VERSIONS_LOCK` to roll back exactly. Details:
[deployment.md](deployment.md#provider-cli-versions-sor-175).

### Evidence policy

- **Stable** = production adapter + pinned runtime **and** a passing
  real-account Modal E2E on the release tag. Fakes, replays and local-only
  runs never count as Stable evidence.
- **Experimental** = the production code path is merged and *some*
  real-account evidence exists (spike or gate harness), but the complete
  release-gate matrix for that provider has not finished.
- **Preview** = the production adapter, image and credential path are
  merged, but *no* real-account evidence exists yet — replay/fixture
  coverage only. Usable, but unverified against a real account.
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
- `deploy.providers` (`SBX_PROVIDERS`) selects which providers a deployment
  serves. Credential prerequisites are scoped to it: `sbx-codex-auth` is
  required only when `codex` is enabled, account Secrets only for enabled
  providers, and the control plane seeds/mounts per enabled provider.
- `POST /v1/accounts/{id}/verify` probes a credential in a throwaway sandbox
  without spending a session.
- **Never** paste credential material into issues, PRs, logs, fixtures, or
  Linear — fixtures use `REDACTED` placeholders; the e2e gates record
  sha256-16 fingerprints only.

## Importing credentials

```bash
# onboarding CLI (SOR-99) — validates the blob, writes the account +
# credential into the store (file store locally; --modal for the workspace Dict)
uv run python -m control.onboarding --modal import \
  --provider codex --from ~/.codex/auth.json

uv run python -m control.onboarding --modal import \
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
| opencode | `.local/share/opencode/auth.json` |

## Local discovery & verification

`sbx credentials` scans the declared paths above under `$HOME` for the
**selected** providers only (`SBX_PROVIDERS` / `deploy.providers`; unselected
providers never block onboarding) and reports one status per provider —
presence, permissions, schema, auth — **never** file contents:

| Status | Meaning |
| --- | --- |
| `not_found` | no declared file exists — hint prints the official login command |
| `permission_invalid` | file is readable by group/other (`chmod 600`, or pass `--allow-open-permissions`) |
| `schema_invalid` | file exists but is not the shape the CLI writes |
| `discovered` | valid private file — *not yet checked against the provider* |
| `verified` | the provider CLI's own auth check accepted it (`--verify`) |
| `auth_invalid` | the provider CLI rejected it (`--verify`) — re-login and re-import |

A plain scan stops at `discovered`: it proves a well-formed private file
exists, not that the provider still accepts it. `sbx credentials --verify`
(also on `sbx init` / `sbx doctor`) runs each provider CLI's own auth
check — `codex login status`, `devin auth status`, `agy models`,
`grok models`, `opencode auth list` — so the provider's answer, not file
shape, decides `verified` vs `auth_invalid`.

The same split exists server-side on `POST /v1/accounts/{id}/verify` /
`python -m control.onboarding verify`: `--probe static` checks blob schema,
`--probe sandbox` proves the blob restores and `runner init` accepts it
(**not** authoritative OAuth verification — the provider is never asked),
and `--probe auth` additionally execs the provider CLI's auth check inside
the throwaway sandbox and takes its answer as authoritative.

Official login commands per provider (what the `not_found`/`auth_invalid`
hints print):

| Provider | Login locally |
| --- | --- |
| codex | `codex login` |
| devin | `devin` (interactive login) |
| antigravity | `agy` (OAuth login) |
| grok | `grok` login |
| opencode | `opencode auth login` |

## Provider-specific notes

**codex.** The reference provider: native events are already canonical and
pass through. `--dangerously-bypass-approvals-and-sandbox` is used because
the sandbox (not Codex's Landlock/seccomp layer) is the security boundary —
see [architecture.md](architecture.md). `CODEX_AUTH_JSON` remains a supported
v1-style credential path for codex only. The RC gate lane is still
**CREDENTIAL_DEFERRED** at `v0.1.1` — the workspace ChatGPT token is stale
and noninteractive refresh fails; restoring the lane needs an interactive
`codex login` (external, not a product defect).

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

**opencode.** The production adapter, pinned image and credential path are
merged and schedulable (`opencode run <prompt> --format json`, resume via
`--session <id>`). The Release 0.1 real-Modal gate passed on the isolated
RC plane: two turns on one native session, cancel, honest usage, zero
credential leaks (`docs/reviews/release-0.1-gate-core.md`) — promoted from
Preview to Experimental. Note the gate ran on the account's OpenAI OAuth
channel (`openai/gpt-5.6-luna`); the OpenCode Zen channel resolves but the
seeded account carries no Zen balance, and `SBX_OPENCODE_MODELS` remains
the per-deploy override for the advertised defaults.

**claude.** Not supported in `v0.1.1` — the experimental adapter seam
is merged but deliberately not registered, so `provider=claude` is not
schedulable (`invalid_provider`). Credential import is possible behind
`--experimental` for early testing only.
