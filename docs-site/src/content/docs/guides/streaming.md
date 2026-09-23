---
title: Streaming events
description: Watch real-time events with Server-Sent Events and reconnection.
---

## Event stream basics

Every run emits canonical events over **Server-Sent Events (SSE)**. Events are streamed as JSON lines with stable IDs for resumption.

```bash
GET /v1/agents/{id}/runs/{runId}/stream
Authorization: Bearer sbx_<key>
```

Each SSE frame carries:
- **id** — line number in `events.jsonl` (use in `Last-Event-ID` to resume)
- **event** — canonical event type
- **data** — JSON payload

Example:

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

id: 4
event: item.started
data: {"item":{"id":"item_0","type":"command_execution","command":"/bin/bash -lc \"echo hello\""}}

: keepalive

id: 5
event: item.completed
data: {"item":{"id":"item_0","type":"command_execution","exit_code":0,"aggregated_output":"hello","status":"success"}}

id: 6
event: turn.completed
data: {"usage":{"input_tokens":2100,"cached_input_tokens":1000,"output_tokens":450}}

id: 7
event: sbx.turn_finished
data: {"status":"success","exit_code":0,"duration_s":2.5,"usage":{"input_tokens":2100,"cached_input_tokens":1000,"output_tokens":450}}
```

## Event types

See [Event Reference](/reference/events/) for the canonical event catalog.

## Keepalives and timeouts

Every 15 seconds of inactivity, the server sends:

```text
: keepalive
```

This keeps the connection alive and helps detect dead links. If you don't see a frame (event or keepalive) for 60+ seconds, reconnect.

## Reconnection with Last-Event-ID

If the connection drops, reconnect and resume from where you left off:

```bash
curl -X GET \
  "$SBX_BASE_URL/v1/agents/{id}/runs/{runId}/stream" \
  -H "Authorization: Bearer sbx_<key>" \
  -H "Last-Event-ID: 5"
```

The server resumes from event 6 (the next one after `id: 5`).

**Bounded reconnect:** Most clients should reconnect at most 5–10 times. After repeated failures, fall back to polling `GET /v1/agents/{id}/runs/{runId}` to read the durable run state.

## Python client example

```python
from examples.sbx_client import SbxClient

client = SbxClient(api_key="sbx_...", base_url="$SBX_BASE_URL")

agent = client.create_agent(text="...", provider="codex")
agent_id = agent["agent"]["id"]
run_id = agent["run"]["id"]

# Watch events with automatic Last-Event-ID reconnect
for event in client.watch(agent_id, run_id, read_timeout_s=120):
    print(f"{event.type}: {event.data}")
    if event.type == "sbx.turn_finished":
        print(f"Turn finished: {event.data['status']}")
        break

# Fall back to polling if watch exhausts retries
run = client.wait(agent_id, run_id=run_id)
print(f"Final status: {run['status']}")
```

The client:
- Handles SSE parsing
- Tracks `Last-Event-ID` automatically
- Retries up to a limit with exponential backoff
- Falls back to `GET /v1/agents/{id}/runs/{runId}` after retries exhaust
- Never re-sends a received event (deduplicated by ID)

## curl example

```bash
# Stream events with curl -N (no buffering)
curl -N \
  "$SBX_BASE_URL/v1/agents/{id}/runs/{runId}/stream" \
  -H "Authorization: Bearer sbx_<key>"

# Resume from event 5
curl -N \
  "$SBX_BASE_URL/v1/agents/{id}/runs/{runId}/stream" \
  -H "Authorization: Bearer sbx_<key>" \
  -H "Last-Event-ID: 5"
```

## JavaScript in the browser

Browser `EventSource` cannot send `Authorization` headers, so fetch + custom parsing is required:

```javascript
const apiKey = process.env.SBX_API_KEY;
const baseUrl = process.env.SBX_BASE_URL;

async function watchRun(agentId, runId, lastEventId = null) {
  const headers = {
    "Authorization": `Bearer ${apiKey}`,
  };
  if (lastEventId) {
    headers["Last-Event-ID"] = lastEventId;
  }

  const response = await fetch(
    `${baseUrl}/v1/agents/${agentId}/runs/${runId}/stream`,
    { headers }
  );

  if (!response.ok) {
    throw new Error(`${response.status}: ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop(); // retain incomplete line

    for (const line of lines) {
      if (line === "") continue;
      if (line.startsWith(":")) continue; // keepalive
      console.log(line);
    }
  }
}

// Note: Standard fetch + EventSource cannot send Authorization.
// Use fetch (as above), a library like `eventsource` with custom
// headers (Node.js), or proxy through a Bearer-aware gateway.
```

## Polling fallback

If SSE is unavailable or exhausts reconnects, poll the run:

```bash
GET /v1/agents/{id}/runs/{runId}
```

The response includes `status` and, once terminal, structured `error`. This is the durable source of truth.

See [Errors](/reference/errors/) for error codes that can occur mid-stream.
