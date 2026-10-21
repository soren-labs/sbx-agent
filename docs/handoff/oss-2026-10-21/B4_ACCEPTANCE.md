# B4 acceptance record: external MCP entry point

Status: **implemented; automated checks recorded below; real-client connect/invoke probe passed
(auth-negative, §2a); remote-task acceptance pending B2/B3** (requires the B2 resident machine and the Mac client run; see §4).

Scope: `sbx mcp serve` (stdio), eleven tools, the B5 SDK/API consistency changes needed for
idempotent MCP mutations, docs and tests. No frontend, OpenAPI route removal, migration or
runtime protocol change.

## 1. Code and contract (declared)

- `src/sbx/mcp_server/` (server, tools, errors); `src/sbx/cli.py` (`mcp serve` only).
- `pyproject.toml` / `uv.lock`: `mcp>=2.3,<3` added as a runtime dependency. Existing pins are
  unchanged (lock diff is additions only; verified with a scratch resolution first).
- SDK: `Sessions.create`, `Turns.cancel`, `Turns.retry` take `idempotency_key`;
  `Sessions.events_page`, `Messages.list(turn_id=)`, `SBXClient.executor_backends()`.
- API (additive): optional `turn_id` filter on `GET /api/sessions/{id}/messages`;
  `docs/specs/unified/openapi.yaml` regenerated (drift test passes).
- Docs: `docs/specs/unified/mcp.md`, `console-sdk.md` section, `docs-site` guide and sidebar,
  CHANGELOG.

## 2. Automated checks (run here, Linux, isolated API)

| Check | Result |
| --- | --- |
| `make lint` | passed (ruff check + format) |
| `tests/integration/mcp_server` (real `sbx mcp serve` subprocess, live uvicorn API, fake OpenCode CLI) | 12 tests: discovery and annotations; create, result, follow-up, cancel across Turns; replay and key conflict; history paging; task pagination; subscription forwarded not converted; revoked key; missing credentials; client.json reuse; unreachable API; legacy handshake; modern `server/discover`; lost-response retry through the SDK |
| `tests/unit/test_mcp_schemas.py` | 7 passed |
| `tests/unit/test_openapi_drift.py` | passed |
| `make test` | see the final report in `b4-complete.txt` |

Protocol probes (manual, same build): legacy `initialize` then `tools/list`; modern
`server/discover` plus `tools/list` with `_meta`; EOF immediately after requests still delivers
every owed answer before exit (exit 0); a stdout line is always JSON; diagnostics go to stderr.

## 2a. Real client probe (Mac, auth-negative; reported by the coordinator)

- Codex CLI 0.162.1 and Claude Code 2.1.292 each connected to `sbx mcp serve` and invoked
  `sbx_list_harnesses`. With an empty SBX client configuration both received the intended
  `unauthenticated` result; Claude Code reported the sbx server as connected.
- No SBX credentials were used. Raw sanitized probe artifacts are kept on the Mac only.
- Scope: this proves the real clients connect and call a tool. It is not full B4 remote-task
  acceptance. The negotiated MCP protocol revision is **not** claimed: it was not captured.

## 3. What the automated tests do not prove

- Full remote-task acceptance with real Codex or Claude Code (create, follow-up, result, cancel on
  a real resource). The auth-negative connect/invoke probe in §2a does not cover these.
- The MCP protocol revision each real client negotiates (not captured).
- Subscription and resident execution through MCP: real resident subscription task, follow-up,
  result and cancel remain pending B2/B3 integration. The tests only prove a missing slot is not
  converted into an API-key call.
- Resident machines (B2). The resident path is gated: it is refused locally unless the deployment
  advertises `resident`, and the B2 routes are called directly only after that check. Not exercised
  on this base.
- Long-running work across a client restart or a day-scale run (B6).

## 4. Real acceptance recipe (Mac Codex, after B2/B3 integration)

1. Sign in once: `sbx auth login` (write scope; stored 0600 in `client.json`).
2. Codex: `codex mcp add sbx -- sbx mcp serve`; Claude Code: `claude mcp add sbx -s user -- sbx mcp serve`.
3. In each client, in a new session: `sbx_list_harnesses`, then `sbx_list_resources`.
4. Create a task with an explicit route (custom_api Connection, or subscription Machine Slot on
   modal, or resident machine when advertised). Record the `session_id`, `turn_id`, and
   `idempotency_key`.
5. Poll `sbx_get_task` until `terminal`; read `sbx_get_result`; send a follow-up with a new key;
   cancel a long-running Turn and wait until it is terminal before recording the cancel result.
6. Start a fresh conversation in the other client and list and query the same Session with
   `sbx_list_tasks` and `sbx_get_task`.
7. Record for each client: the protocol revision observed (first frames), each tool call result,
   and the `request_id` of any error. Keep credentials out of the record.

Attach the evidence to this file as a new section; do not mark the client rows passed on fakes.

## 5. Compatibility and limits

- Existing API/SDK/CLI defaults are unchanged; the strict explicit selection applies to MCP only.
- Result size: 64 KiB per call; larger output is windowed or refused, never truncated silently.
- Messages reads are bounded by Turn only through the new `turn_id` filter. A Session-wide read
  without the filter is unchanged and still unbounded (B5 follow-up if needed).
