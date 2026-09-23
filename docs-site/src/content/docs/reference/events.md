---
title: Events
description: Canonical event types, shapes, and stream format.
---

## Event stream format

Events are streamed as Server-Sent Events (SSE) with ID, type, and data:

```text
id: 1
event: sbx.session_meta
data: {"provider":"codex","account_id":"codex-1"}

id: 2
event: sbx.turn_started
data: {"n":1}

id: 3
event: turn.started
data: {}

: keepalive
```

Each event has:
- **id** — line number in `events.jsonl` (use in `Last-Event-ID` to resume)
- **event** — type (canonical name)
- **data** — JSON payload

Keepalive (`: keepalive`) is sent every 15 seconds with no data.

## Canonical events

Codex-shaped provider-agnostic events:

| Type | Shape | When |
| --- | --- | --- |
| `thread.started` | `{thread_id}` | Once per agent (session identifier) |
| `turn.started` | `{}` | Per turn, before CLI execution |
| `item.started` | `{item: {...}}` | Item enters in-progress |
| `item.updated` | `{item: {...}}` | Item incremental update (rare) |
| `item.completed` | `{item: {...}}` | Item reaches terminal state |
| `turn.completed` | `{usage: {...}}` | Turn succeeded with token usage |
| `turn.failed` | `{error: {message}}` | Turn failed (non-fatal error) |
| `error` | `{message}` | Fatal stream error (unrecoverable) |

## Runner events

Control plane events:

| Type | Shape | When |
| --- | --- | --- |
| `sbx.session_meta` | `{provider, model, account_id}` | Once per agent (turn 1 startup) |
| `sbx.turn_started` | `{n}` | Turn n started (before provider CLI) |
| `sbx.turn_finished` | `{status, exit_code, duration_s, usage}` | Turn completed or failed |
| `sbx.error` | `{message}` | Runner error (stream corruption, timeout) |

### Turn finish statuses

| Status | Exit code | Meaning |
| --- | --- | --- |
| `success` | 0 | Turn completed successfully |
| `codex_error` | 2 | Provider CLI exited non-zero |
| `timeout` | 3 | Turn exceeded `SBX_TURN_MAX_SECONDS` |
| `bad_json` | 4 | Event stream was malformed |
| `auth_invalid` | 5 | Credential or auth check failed |

## Items

Item payloads in `item.started` / `item.completed`:

### agent_message

```json
{
  "id": "item_0",
  "type": "agent_message",
  "text": "I'll create a function for email validation..."
}
```

**Note:** Only `item.completed` is sent for messages (no `item.started`).

### command_execution

```json
{
  "id": "item_0",
  "type": "command_execution",
  "command": "/bin/bash -lc \"npm test\"",
  "aggregated_output": "PASS 4/4\n",
  "exit_code": 0,
  "status": "success"
}
```

**Note:** `item.started` has `exit_code: null`. Both `status` (success/nonzero/timeout/etc.) and `exit_code` are present.

### file_change

```json
{
  "id": "item_0",
  "type": "file_change",
  "changes": [
    {"path": "src/validate.js", "kind": "created"},
    {"path": "src/index.js", "kind": "modified"}
  ],
  "status": "success"
}
```

### reasoning

```json
{
  "id": "item_0",
  "type": "reasoning",
  "text": "I need to handle edge cases for international email formats..."
}
```

### error (non-fatal)

```json
{
  "id": "item_0",
  "type": "error",
  "message": "Module not found: crypto"
}
```

**Note:** Item-level errors are different from fatal stream `error` events.

## Usage fields

Present in `turn.completed` and `sbx.turn_finished`:

| Field | Always? | Meaning |
| --- | --- | --- |
| `input_tokens` | ✅ | Prompt tokens |
| `cached_input_tokens` | ✅ | Reused prompt cache tokens |
| `output_tokens` | ✅ | Completion tokens |
| `cache_write_input_tokens` | ❌ | Tokens written to cache this turn |
| `reasoning_output_tokens` | ❌ | Internal reasoning token budget (extended thinking) |

If a provider doesn't report a field, it defaults to `0`.

Example:

```json
{
  "usage": {
    "input_tokens": 4200,
    "cached_input_tokens": 2100,
    "output_tokens": 850,
    "cache_write_input_tokens": 200,
    "reasoning_output_tokens": 0
  }
}
```

## Event consumption

### Parsing

Events are LF-delimited. A simple parser:

```python
import json


def parse_sse(stream_lines):
    event_id = None
    event_type = None
    event_data_lines = []

    for line in stream_lines:
        if not line.strip():
            # Blank line: end of frame
            if event_type is not None:
                data_text = "\n".join(event_data_lines)
                try:
                    data = json.loads(data_text)
                except json.JSONDecodeError:
                    data = {"raw": data_text}
                yield {"id": event_id, "type": event_type, "data": data}
            event_id = None
            event_type = None
            event_data_lines = []
        elif line.startswith(":"):
            # Comment (keepalive): skip
            continue
        elif line.startswith("id:"):
            event_id = line[4:].strip()
        elif line.startswith("event:"):
            event_type = line[7:].strip()
        elif line.startswith("data:"):
            event_data_lines.append(line[6:].strip())
```

### Resumption

If disconnected, reconnect with `Last-Event-ID`:

```bash
curl "..." \
  -H "Last-Event-ID: 5"
```

The server resumes from event ID 6 (next after 5).

### Terminal detection

A run ends when `sbx.turn_finished` is received (or `sbx.error` for fatal errors). After that, the SSE stream closes.

Polling `GET /v1/agents/{id}/runs/{runId}` returns the durable terminal state (status, error, usage).
