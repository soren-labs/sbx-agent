# External MCP entry point (`sbx mcp serve`)

Status: implemented for B4 (2026-10-21). Automated tests cover a real stdio subprocess against
a live isolated API. Real Codex and Claude Code acceptance is **not** claimed here; it is
recorded separately in `docs/handoff/oss-2026-10-21/B4_ACCEPTANCE.md`.

## Shape

- `sbx mcp serve` runs a stdio MCP server (`sbx` package, `sbx.mcp_server`). The client launches
  it as a subprocess.
- It uses the same `ClientConfig` (`$XDG_CONFIG_HOME/sbx/client.json`, or `SBX_BASE_URL`,
  `SBX_API_KEY`, `SBX_WORKSPACE_ID`) and the same public SDK (`sbx.sdk`) as the CLI. It has no
  database or runtime access and no business logic of its own.
- stdout carries MCP JSON-RPC only; diagnostics go to stderr. Closing stdin ends the server
  after every owed answer is written. The server never prompts.
- Protocol: MCP current revision `2026-07-28` (per-request `_meta`, `server/discover`) and the
  legacy `2025-11-25` handshake (`initialize`). One server process serves one connection; the era
  is fixed per connection. Tests cover both.
- Built on the official `mcp` Python SDK (`>=2.3,<3`), low-level `Server` API, explicit JSON
  Schemas (`additionalProperties: false`) and `outputSchema`.

## Authentication

Each tool call reads the configuration and builds a fresh client, so a revoked key fails on the
next call. Missing credentials return `unauthenticated` with `details.source = "client_config"`
and an action to run `sbx auth login`. Nothing prompts. Keys are never echoed in results, errors
or logs. Discovery and every action run as the key's principal and obey workspace access.

## Tools

| Tool | Effect | Annotations (read-only / destructive / open-world) | Notes |
| --- | --- | --- | --- |
| `sbx_list_harnesses` | none | read-only | Harness manifests; `startable` when support tier is `supported`. |
| `sbx_list_resources` | none (probe is a live check) | read-only / – / open-world | Per harness: API-key routes (`usable` when the API says compatible and healthy) and Machine Slot routes (`usable` when `ready`). Enabled executor backends. Resident machines only when `resident` is advertised (`resident_executor`). `probe=true` runs a bounded live reachability check. |
| `sbx_list_tasks` | none | read-only | Sessions, keyset pages (`limit` ≤ 50, `next_cursor`). |
| `sbx_create_task` | runs work | side-effecting / destructive / open-world | Returns `session_id`, `turn_id` at once. |
| `sbx_get_task` | none | read-only | Session summary, `active_turn_id`, optional Turn state. |
| `sbx_get_events` | none | read-only | One bounded page: forward with `after`, history with `before`, optional `turn_id` and `types`. `has_more` is a hint. |
| `sbx_get_result` | none | read-only | Text of exactly one Turn's output, in `max_chars` windows from `offset`. `text` is null until the Turn is terminal. |
| `sbx_send_followup` | runs work | side-effecting / destructive / open-world | Queues a message; it runs as a new Turn. |
| `sbx_cancel_task` | stops work | side-effecting / destructive | `cancel_pending` until the API reports a terminal state. Partial file changes may remain. |
| `sbx_retry_task` | runs work | side-effecting / destructive / open-world | Only when the Turn's `actions` include `retry`. |
| `sbx_acknowledge_unknown` | records a decision | side-effecting | Only when the Turn's `actions` include `acknowledge_unknown` (outcome could not be proven). |

### `sbx_create_task` input policy

Billing and resource intent is explicit; nothing is chosen on the caller's behalf.

- `inference_mode` is required. `custom_api` needs `inference_connection_id` and must not name a
  Machine Slot. `subscription` needs `machine_slot_id`, must not name a Connection, and runs on
  `executor_backend` `modal`. A subscription request never becomes an API-key call.
- `executor_backend` is required (`local`, `modal`, or `resident`). `resident` needs
  `resident_machine_id` and is refused, without calling the API, unless the deployment advertises
  the resident backend.
- `harness_id` is required; `model` and `effort` are optional and forwarded unchanged.
- `idempotency_key` (8–200 characters) is required. Reuse the same key with the same body to
  retry; the same key with a different body returns `idempotency_conflict`.

The API keeps its existing defaults for other entry points. This policy applies to MCP only.

## Results, errors and bounds

- Successful results are `structuredContent` plus one JSON text block of the same data.
- Failures are tool errors (`isError: true`) with `structuredContent`:
  `{code, category, message, status, request_id, retryable, action, details}`.
  `category` is one of `config`, `reachability`, `authentication`, `executor` (the same as
  `sbx status`), or `api` / `request` for other codes.
- Argument errors return `validation_failed` with `details.fields` (names only, never values).
  An unknown tool name is a JSON-RPC error (`-32602`).
- Forwarded API codes keep their meaning (for example `not_found`, `idempotency_conflict`,
  `invalid_transition`, `outcome_unknown`, `connection_required`, `unsupported_capability`).
- A non-reachable API returns `transport_error` with the action to check `SBX_BASE_URL`.
- No result exceeds 64 KiB. An over-long result is refused with `result_too_large`; it is never
  truncated silently.
- Pagination: task lists by keyset cursor; events by `after`/`before` cursors; results by
  `offset`/`next_offset`.
- Long work is never held open: creation, follow-up and cancellation return at once. Poll
  `sbx_get_task` and `sbx_get_result`.

## Idempotency

- Mutations that create or send work (`sbx_create_task`, `sbx_send_followup`, `sbx_retry_task`,
  `sbx_cancel_task`) require a caller-supplied `idempotency_key`. The SDK sends it only in the
  `Idempotency-Key` header, never in the JSON body.
- Transport retries inside the SDK reuse one key. A lost response followed by a retry with the
  same key returns the original result (one Session, one Turn), as tested.
- `sbx_acknowledge_unknown` takes no key; the API route takes none.

## Compatibility notes

- Resident machines (B2) are not in this B4 base. The resident path is written against the B2
  contract (`GET /api/workspaces/{id}/resident-machines`, `executor.resident_machine_id`) but
  stays gated until the deployment advertises `resident`.
- `sbx_list_resources` returns Machine Slot and API-key routes as the API reports them. A `ready`
  or `usable` flag is never an end-to-end proof for a subscription or a remote machine.
- MCP never switches a Session between subscription and API-key billing on failure.
- Changed API surface in this change: `GET /api/sessions/{id}/messages` accepts an optional
  `turn_id` filter (additive; without it the response is unchanged). Recorded in
  `docs/handoff/oss-2026-10-21/` for B5 review.
