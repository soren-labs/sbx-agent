CREDENTIAL_DEFERRED: claude real auth/gate unavailable

# SOR-97 — Claude Code Experimental spike (Release 0.1/A2)

Author note for the deterministic/product-code portion. The local CLI is
logged in via an **OS-keyring OAuth token**, which is not file-portable,
and the managed account behind it returns `401 ERROR_ACCOUNT_CLOSED` on
real API calls — no legitimate, movable credential exists on this
machine. Evidence below is therefore **fake/replay plus real-CLI
*shape* captures only**: every behavioural claim about failure paths
comes from live `claude` 2.1.250 probes (fresh-HOME, bogus `--resume`),
and the success-path event shape is built from the same verified line
schema plus on-disk session transcripts. **Do not label this provider
Stable** and do not count it toward the SOR-95 real-gate evidence until
a real credential runs the Modal ≥2-turn E2E. Provider stays
`Experimental`: it is deliberately absent from the frozen
`adapter.py` `PROVIDERS`/`_REGISTRY`.

## Support-matrix data

| Field | Value |
| --- | --- |
| provider id | `claude` (Experimental; not registered) |
| adapter | `runtime/runner/adapters/claude.py` (`ClaudeAdapter`) |
| CLI | `claude` 2.1.250, native ELF `~/.local/share/claude/versions/2.1.250` (`~/.local/bin/claude` symlink) |
| credential file | `.claude/.credentials.json` (mode 600, `$HOME`-relative) — **verified**: a blob placed there is honoured by `claude auth status` (`loggedIn: true`, `authMethod: claude.ai`) |
| first turn | `claude -p <PROMPT> --output-format stream-json [--model <M>] --verbose --permission-mode bypassPermissions` |
| resume | same + `--resume <session_id>` (model re-read from session.json); `--fork-session` is **not** used — resume must keep the id |
| session id | UUID on `system.init.session_id` (also top-level on every stream line) → `thread.started` |
| stdin | DEVNULL (runner contract); `-p` never reads stdin |
| cancel | existing `runner stop` path (SIGTERM → SIGKILL), no adapter changes |
| env scrub (wired) | `CLAUDE_ENV_EXCLUDE` in `runtime/runner/credentials.py`, unioned into `AGENT_ENV_EXCLUDE`: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `ANTHROPIC_DEFAULT_*_MODEL`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY}`, `CLAUDE_CONFIG_DIR` (so the CLI always resolves `~/.claude` under the sandbox HOME — `GROK_HOME`/`GROK_CONFIG_PATH` precedent) — every alternate auth/model channel is stripped from the child env by `codex.child_env` so the restored file is the only credential source |

## Auth findings (verified locally, no credential values read)

- `claude auth status` prints a JSON object: `loggedIn`, `authMethod`
  (`oauth_token` / `claude.ai` / `none`), `apiProvider`,
  `projectsDirectory`, plus identity fields when real.
- Credential channels, in the order the CLI resolves them: **OS keyring**
  (this machine — `secret-tool`/D-Bus present; **not file-portable**,
  cannot be moved into a sandbox) → **`~/.claude/.credentials.json`**
  (the file channel; verified honoured by `auth status`) → env
  (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) → `apiKeyHelper` in
  settings → 3P providers (Bedrock/Vertex/Foundry envs).
- `--bare` restricts auth to `ANTHROPIC_API_KEY`/`apiKeyHelper` and
  **never reads OAuth** — deliberately *not* on argv: our declared
  channel is the OAuth file. Documented so a future "tighten" change
  doesn't silently break the credential blob.
- Fresh `HOME` + stripped env: `auth status` → `loggedIn: false`,
  `authMethod: none`, rc=1. CLI creates `~/.claude/`, `~/.claude.json`
  (600), `~/.claude.json.lock` on first touch — `$HOME` only needs to
  be writable; `prepare_home` just makes `~/.claude` 700 and pins the
  credential file to 600.
- Token **refresh/rewrite behaviour is unknown** (the keyring path never
  exercises the file). `export-credentials` roundtrip already covers a
  rewritten `.credentials.json` generically; verify on the real gate.

## Real-capture event shape (2.1.250)

- `{"type":"system","subtype":"init","session_id","model","tools",…,
  "apiKeySource","claude_code_version"}` — always first; allocates the
  session UUID even when auth is missing.
- `{"type":"assistant","message":{"content":[blocks]},…,"session_id"}` —
  blocks: `thinking`/`text`/`tool_use` (transcript-verified); API/auth
  failures arrive as synthetic assistant messages with
  `is_api_error_message: true`, `error` (`authentication_failed`,
  `rate_limit`, `unknown`) and the failure text as the content.
- `{"type":"user","message":{"content":[{"type":"tool_result",
  "tool_use_id","content","is_error"}]}}` — closes a `tool_use`.
- `{"type":"result","subtype","is_error","result","errors","usage",
  "session_id"}` — terminal. `usage` real fields:
  `input_tokens`/`cache_read_input_tokens`/
  `cache_creation_input_tokens`/`output_tokens`/
  `output_tokens_details.thinking_tokens`.
- **The CLI exits rc=1 on every failure probed** (independently
  re-verified on this machine's 2.1.250 during review): no-auth emits
  `assistant` text `Not logged in · Please run /login` then
  `result{is_error:true, terminal_reason:"api_error"}`, rc=1; a bogus
  `CLAUDE_CODE_OAUTH_TOKEN` fails `API Error: 401 Invalid bearer
  token`, rc=1; stale `--resume <bogus-uuid>` prints `No conversation
  found with session ID: …` on stderr and emits
  `result{subtype:"error_during_execution", errors:[…]}` on stdout,
  rc=1. Auth validity is still a *stream* property — the terminal
  `result` keeps `subtype:"success"` with `is_error:true` — but the
  exit code is *not* masked on this build.
- This machine's managed proxy account fails real calls with
  `API Error: Connect error 401: ERROR_ACCOUNT_CLOSED [unauthenticated]`
  — captured as the `auth_invalid`-family shape; no bypass attempted.

## Event translation (native stream-json → canonical)

- `system` `subtype=init` → `thread.started{thread_id: session_id}` +
  `turn.started`; any line's top-level `session_id` is the marker
  (events.md rule 1). Other `system` subtypes → NOOP.
- `assistant` → `thinking`→`reasoning`, `text`→`agent_message`
  (both `item.completed`), `tool_use`→`item.started` keyed by block `id`
  (`Write`/`Edit`/`MultiEdit`/`NotebookEdit`→`file_change`, `Bash` et al.
  →`command_execution`). `is_api_error_message`/`error` lines →
  `error{message}` (turn still closes on `result`).
- `user` → `tool_result`→`item.completed` paired by `tool_use_id`
  (first-sight emits started+completed; `is_error`→failed).
- `result` → `is_error` or non-`success` subtype → `error`+`turn.failed`;
  else `turn.completed{usage}` (five canonical fields mapped above).
  An error `result` suppresses the session marker — a failed resume
  echoes the *requested* id and must not adopt it, so the runner's
  replacement-id check also fails the turn if a build ever masks the
  failure with rc=0 (verified defensively; real 2.1.250 exits rc=1).
- `stream_event` (partials; never requested — no
  `--include-partial-messages` on argv) and unknown parseable kinds →
  NOOP (SOR-80); only non-JSON-object lines count as bad JSON.
- `health_from`: translate-time auth/rate flags outrank the exit code,
  then rc=0 → `ok`, else stderr needles (`not logged in`/`401`/`429`/
  `rate limit`/`overloaded`…), else `unknown`.

## Deterministic evidence

- `tests/unit/runner/test_claude_adapter.py` — argv/`prepare_home`/
  translate/health against staged real-shape fixtures
  (`tests/unit/runner/fixtures/claude/{success,resume,auth_invalid,
  rate_limited,stale_session,nonzero,badjson}.jsonl`; auth_invalid and
  stale_session mirror real captures verbatim).
- `tests/unit/runner/test_claude_turn.py` — e2e via
  `tests/unit/runner/runner_claude.py` (test-only shim performing the
  registration the frozen-file diff would make) + `replay_claude.py`:
  init layout, credential blob restore (600), argv + stdin DEVNULL,
  resume same-id, real-rc stale resume → exit 2 (plus a defensive
  rc=0 replacement-check variant), real-rc auth-invalid → exit 5
  `auth_invalid` (plus a defensive rc=0 stream-truth variant), hang →
  timeout exit 3, env-scrub spy assertions, badjson → exit 4,
  export-credentials roundtrip.
- `uv run pytest tests/unit/runner` — 223 passed; `ruff check`/`format`
  clean on all new files.

## Known limitations / follow-ups

- **Runner finish-status is rc-derived**: verified 2.1.250 exits rc=1 on
  auth/stale failures, so `sbx.turn_finished.status` correctly reports
  `auth_invalid` (exit 5) / `codex_error` (exit 2) today. Residual risk:
  if a future claude build ever masks a stream-level failure with rc=0,
  the canonical stream still carries `error`+`turn.failed` but the
  finish status would read `success` and `health_from` is never
  consulted (runner short-circuits on rc=0). A small `turn.py`
  hardening (treat a translated `turn.failed`/`error` as terminal)
  stays a contract-change-request candidate — not fixed here (file
  owned by SOR-62/72); defensive rc=0 variants cover the path in tests.
- Registration diff (post-gate, mirrors SOR-96): add `claude` to the
  contract provider set (canonical-yaml/api enums), `PROVIDERS`,
  `_REGISTRY["claude"]`, plus bootstrap account/image wiring.
  (`CLAUDE_ENV_EXCLUDE` is already unioned into `AGENT_ENV_EXCLUDE`.)
- Unknowns pending a real account: `.credentials.json` refresh cadence
  and rewrite-on-refresh (does `expiresAt` refresh mutate the file
  mid-session?), multi-account concurrency limits, Modal image
  packaging (versioned ELF dir vs `npm i -g @anthropic-ai/claude-code`),
  and whether `stream_event` partials are worth enabling.
- Gate to Stable: real subscription/API credential → file-channel
  restore → Modal ≥2-turn E2E + leak/cleanup, per the SOR-95 gate.

## Independent review (Release 0.1)

Reviewer: Devin (independent; did not author the spike). Reviewed
`e1fd427` → `7e35f3c` against the SOR-97 scope.

**Re-verified live on the installed `claude` 2.1.250** (fresh `HOME`,
stripped env, no credential values read): `auth status` →
`loggedIn:false, authMethod:none, rc=1`; no-auth `-p` run emits the
documented init/synthetic-assistant/result shapes and exits **rc=1**;
bogus `CLAUDE_CODE_OAUTH_TOKEN` → `API Error: 401 Invalid bearer
token`, rc=1; stale `--resume` → stderr notice +
`error_during_execution` result, rc=1. **The original note claimed
"rc=0 on every failure observed" — corrected above**; the stream
shapes themselves were accurate and are unchanged.

**Fixes applied during review:**

1. Corrected the exit-code claims in the adapter/replay/test
   docstrings; split the stale-resume and auth-invalid e2e tests into
   real-shape (rc=1 → exit 2 / exit 5 `auth_invalid`) plus explicitly
   defensive rc=0 variants covering the residual masked-rc runner gap.
2. Wired `CLAUDE_ENV_EXCLUDE` into `AGENT_ENV_EXCLUDE`
   (`runtime/runner/credentials.py`, grok/devin precedent) — the child
   env scrub was declared but unenforced; added `CLAUDE_CONFIG_DIR`
   (config-dir redirect would bypass the restored credential file —
   `GROK_HOME`/`GROK_CONFIG_PATH` precedent). The spy now asserts
   `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`/`CLAUDE_CONFIG_DIR`
   never reach the CLI subprocess.
3. Added a hang→timeout (exit 3) e2e covering the cancel path, and an
   `AGENT_ENV_EXCLUDE` membership test.

No real credential exists on this machine for the Modal gate (keyring
OAuth is not file-portable; the managed account returns
`ERROR_ACCOUNT_CLOSED`) — provider stays **Experimental**, honestly
deferred, not faked.
