---
title: Use SBX from Codex and Claude Code (MCP)
description: Configure the sbx MCP server in Codex and Claude Code, start and follow Sessions, read results, cancel, and understand errors.
---

`sbx mcp serve` gives Codex and Claude Code the same SBX API as the CLI and Python SDK. You can
list the resources you may use, start a Session, come back later in a new conversation, read the
result, send a follow-up or cancel.

## 1. Sign in once

The MCP server never prompts. Store a key with the CLI first:

```bash
sbx auth login
```

The key is kept in your private `client.json` (mode `0600`). The default scope lets the server
create and change work; it cannot mint API keys. You can also set `SBX_BASE_URL` and
`SBX_API_KEY` in the client's environment instead.

## 2. Add the server to each client

Codex (CLI):

```bash
codex mcp add sbx -- sbx mcp serve
```

Claude Code, available in all your projects:

```bash
claude mcp add sbx -s user -- sbx mcp serve
```

Start a new session in each client and check that `sbx_list_harnesses` answers. If the client
cannot start the command, make sure `sbx` is on the `PATH` the client sees.

## 3. Choose resources explicitly

1. `sbx_list_resources` shows, per harness, the API-key Connections and Machine Slots that can run
   it, with a `usable` flag from the API. Pass `harness_id` to also see its models.
2. `sbx_create_task` needs every choice spelled out:
   - `inference_mode: "custom_api"` with `inference_connection_id`, which spends an API key; or
   - `inference_mode: "subscription"` with `machine_slot_id` and `executor_backend: "modal"`,
     which uses a subscription login. It never falls back to an API key.
   - `executor_backend` (`local` or `modal`; `resident` only when the deployment offers it).
3. Choose an `idempotency_key` (8–200 characters). Reuse it only to retry the same request.

## 4. Follow the work

`sbx_create_task` returns `session_id` and `turn_id` at once. Then:

- `sbx_get_task` with `turn_id` shows the Turn's state (`terminal` tells you when it has stopped).
- `sbx_get_result` returns that Turn's output. Use `offset` for the next window when `next_offset`
  is set.
- `sbx_get_events` reads committed events a page at a time, for progress.
- `sbx_list_tasks` finds existing work in a new conversation.
- `sbx_send_followup` queues another message on the same Session.
- `sbx_cancel_task` requests a stop. The Turn is only stopped when `terminal` is true; until then
  `cancel_pending` is true.
- If a Turn ends as `outcome_unknown`, call `sbx_acknowledge_unknown` before sending new work.

## Errors

Every failure has a `code`, a `category` and an `action`:

| Category | Typical codes | What to do |
| --- | --- | --- |
| `authentication` | `unauthenticated`, `forbidden` | Run `sbx auth login` or set `SBX_API_KEY`, then restart the client. |
| `reachability` | `transport_error` | Check `SBX_BASE_URL` and run `sbx status`. |
| `config` | `config_invalid` | Repair `client.json`. |
| `executor` | `connection_required`, `unsupported_capability`, `connection_revoked` | Pick another Connection or Slot, or fix the Connection. |
| `request` | `validation_failed`, `result_too_large` | Correct the arguments or lower `limit` / `max_chars`. |

`request_id` is included whenever the API supplied one; quote it when reporting a problem.

## Limits

- Results are capped at 64 KiB per call. Larger output is returned in windows.
- Work runs on the machine and under the limits you chose; a remote machine must be reachable for
  its work to start. Check `sbx_list_resources` with `probe=true` if unsure.
- The server does not hold a call open while work runs; poll instead.
