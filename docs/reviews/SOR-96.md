# SOR-96 — OpenCode provider (Release 0.1/A1)

Author note for the deterministic/product-code portion. Evidence below is
**fake/replay only**: every number comes from `tests/fakes/fake_opencode.py`,
`tests/unit/runner/replay_opencode.py`, or the staged fixtures under
`tests/unit/runner/fixtures/opencode/` and `tests/fixtures/events/opencode/`.

> **CREDENTIAL_DEFERRED: opencode real auth/gate pending**
>
> No real OpenCode credential was used. Real-CLI auth verification, the
> real Modal E2E gate, and the ≥2-turn resume gate are still open — do not
> label this provider Stable and do not count it toward the SOR-95 real-gate
> evidence until those run with a real `auth.json`.

## Support-matrix data

| Field | Value |
| --- | --- |
| provider id | `opencode` |
| adapter | `runtime/runner/adapters/opencode.py` (`OpencodeAdapter`) |
| credential file | `.local/share/opencode/auth.json` (mode 600, XDG data path) |
| first turn | `opencode run <PROMPT> --format json [-m <provider/model>] --dir $SBX_WORK --auto` |
| resume | same + `--session <sessionID>` (model re-read from session.json) |
| session id | native `sessionID` (top level and `part.sessionID`) → `thread.started` |
| default models | `anthropic/claude-sonnet-4.5`, `openai/gpt-5.3-codex` (`SBX_OPENCODE_MODELS` override) |
| seeded account | `opencode-1`, secret `sbx-acct-opencode-1`, `SBX_OPENCODE_*` overrides + `SBX_OPENCODE_ACCOUNTS` JSON fleet |
| image | `sbx-runtime-opencode` = `sbx-runtime` + `npm i -g opencode-ai@<pin>` (`packages.txt` `opencode_*`; `make image-opencode`) |
| sandbox env | `HOME`+XDG pinned to `$SBX_WORK/home` (same pinning as devin) |
| env denylist | `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`, `OPENCODE_SERVER_PASSWORD`, `OPENCODE_SERVER_USERNAME` (plus the shared `SBX_ACCOUNT_*`/`CODEX_*` denylist) |
| cancel | existing `runner stop` path (SIGTERM → SIGKILL), no adapter changes |

## Event translation (native `--format json` → canonical)

- `step_start` → `turn.started` once (multi-step tool loops re-emit; later
  ones are NOOP). First `sessionID` sighting → `thread.started`.
- `reasoning` / `text` → `item.completed` `reasoning` / `agent_message`;
  `part.text` is cumulative, so the latest snapshot wins (never
  concatenated). Buffered parts flush at `tool_use` / `step_finish` /
  `error` boundaries.
- `tool_use` → `item.started` on first sight of `callID`
  (`state.status` pending/running); `item.completed` on `completed`/`error`.
  First-sight terminal emits started+completed (WP0 fixture shape). File
  tools (`edit`/`write`/`patch`/…) → `file_change`; `bash` and the rest →
  `command_execution` with `state.input.command` / `state.output` /
  `state.metadata.exit`.
- `step_finish` → `reason == "tool-calls"` is an intermediate boundary
  (NOOP); any other reason closes the turn. `part.tokens` accumulates
  across all step_finish parts into the canonical five usage fields
  (`input`/`output`/`reasoning`/`cache.read`/`cache.write`).
- `error` → `error{message}` + `turn.failed`; reads
  `error.data.message` (real `APIError`) and flat/nested forms.
- Unknown parseable kinds → NOOP (SOR-80 forward compatibility); only
  non-JSON-object lines count as bad JSON (exit 4).
- Stale `--session` id: mismatching `sessionID` suppresses
  `thread.started` and fails the turn (exit 2) — never adopts a
  replacement session.
- `health_from`: `0`→ok; stderr `401`/`incorrect api key`/`unauthorized`/
  `auth`→`auth_invalid` (exit 5); `429`/`rate limit`/`quota`/
  `resource_exhausted`/`overloaded`→`rate_limited`; else `unknown`.
  Because `--format json` routes fatal errors to the stdout `error`
  event only (the real CLI skips `UI.error` once `emit` succeeds),
  `health_from` falls back to the native `error` messages seen on the
  translated stream when stderr has no needles (review fix).

## Deterministic evidence

- `tests/unit/runner/test_opencode_adapter.py` — argv/`prepare_home`/
  translate/health against staged + WP0 fixtures.
- `tests/unit/runner/test_opencode_turn.py` — `python -m runtime.runner`
  e2e via `replay_opencode.py` and `fake_opencode.py`: init layout,
  credential blob restore/export (600), env scrubbing, argv + stdin
  DEVNULL, success/resume/stale-resume/nonzero/auth_invalid(5)/badjson(4)/
  timeout(3).
- `tests/unit/api_v1/test_bootstrap.py` — opencode seeded account,
  auto/named scheduling, model metadata.
- `tests/integration/runtime/test_provider_images.py` — `IMAGE_BUILDERS`
  entry, npm pin seam, Makefile target, config name sync.

## Independent review (Release 0.1, post-`e1fd427`)

Scope checked against SOR-96: adapter argv/event translation was verified
against `opencode-ai@1.18.29` source (`cli/cmd/run.ts`): `run [message..]`,
`--format json`, `-m`, `-s/--session`, `--dir`, `--auto` (auto-approve) all
exist; the emitted stream carries top-level `sessionID` on every line,
`tool_use` only at terminal `state.status`, `error` as
`{error:{name,data:{message}}}`, and a stale `--session` exits rc=1 with
stderr "Session not found" and empty stdout. Pin `1.18.29` was published
2026-09-04 (>7 days). Credential restore/scrub/export, scheduler seeding,
named image + HOME/XDG pinning, and env scoping match the contract. No
credentials, tokens, or real captures in the tree; the real-Modal gate
stays honestly deferred above.

One blocker found and fixed in review: under `--format json` the real CLI
routes fatal errors (401/429) to the stdout `error` event only — stderr
carries no needles — so a real auth failure classified `unknown`/exit 2
instead of `auth_invalid`/exit 5, and the prior e2e masked it with a
fabricated stderr line. `health_from` now falls back to the native
`error` messages seen on the stream; `test_auth_invalid_exits_5` replays
the real shape (stdout error + rc=1, clean stderr) and unit tests cover
the stream fallback plus the stale-stream `unknown` boundary. Also
deduped repeat `error` events in the defensive stale path.

Verification: `make lint` clean; `make test` 1064 passed, 1 skipped.
