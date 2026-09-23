---
title: Python client
description: API reference for examples/sbx_client.py.
---

## Installation

The Python client is in `examples/sbx_client.py`. No external dependencies beyond `httpx`:

```python
from examples.sbx_client import SbxClient

client = SbxClient()
```

`SbxClient()` reads `SBX_BASE_URL` and `SBX_API_KEY` from the environment. The built-in default for `SBX_BASE_URL` is the maintainers' deployment (`https://sbx.sorenforge.com`); self-hosters must set `SBX_BASE_URL` to their own control plane.

## Creating agents

```python
agent = client.create(
    text="Write a function that validates emails",
    provider="codex",
    model="gpt-5-turbo",
    account_id="auto",
    reasoning_effort="medium",  # optional
)
```

Returns an `Agent` object with `id`, `status`, `created_at`, and `runs`.

## Follow-up runs

```python
run = client.followup(
    agent.id,
    text="Now add async support",
)
```

## Waiting for completion

```python
run = client.wait(
    agent.id,
    run_id=None,  # omit for latest run
    timeout_s=300,
)
# run.status is terminal: FINISHED, ERROR, CANCELLED, EXPIRED, or UNKNOWN
print(f"Status: {run.status}")
if run.error:
    print(f"Error: {run.error.code} ({run.error.source})")
```

## Watching events

```python
for event in client.watch(agent.id, run.id, read_timeout_s=120):
    print(f"{event.type}: {event.data}")
    if event.type == "sbx.turn_finished":
        break
```

Returns `SseEvent` objects with `id`, `type`, `data`. Automatically reconnects with `Last-Event-ID` if the stream drops.

## Cancelling

```python
client.cancel(agent.id, run.id)
```

## Closing agents

```python
client.close_agent(agent.id)
```

## Usage & costs

```python
usage = client.usage(agent.id)
for run_usage in usage.runs:
    print(f"Run {run_usage.id}: {run_usage.cost_estimate_usd} USD")
print(f"Total: {usage.total_cost_estimate_usd} USD")
```

## Artifacts

```python
artifact = client.create_artifact(
    agent.id,
    test_command="npm test",
)

artifacts = client.artifacts.list(
    agent_id="agent_xyz",
    run_id="run_abc",
)

client.artifacts.download(agent_id, artifact.id, "artifact.zip")
```

## Workflow recovery

```python
workflow = client.recover(workflow_id="my-task-2026-09-23")
for agent in workflow.agents:
    print(f"Agent {agent.id}: {agent.status}")
```

## Error handling

```python
from examples.sbx_client import SbxApiError, SbxTransportError

try:
    agent = client.create(...)
except SbxApiError as e:
    print(f"API error: {e.status} {e.code} {e.message}")
    if e.retry_after:
        print(f"Retry after: {e.retry_after}s")
except SbxTransportError as e:
    print(f"Transport error: {e.method} {e.path}")
    if e.check:
        print(f"Run this to verify: {e.check}")
    if e.idempotent:
        print("Safe to retry (idempotent)")
```

## Methods (full reference)

| Method | Returns | Notes |
| --- | --- | --- |
| `create(...)` | Agent | Starts agent + run 1 |
| `followup(agent_id, ...)` | Run | New run on idle agent |
| `list_agents(workflow_id?, limit?)` | list[Agent] | Optionally filter by workflow |
| `get_agent(id)` | Agent | — |
| `close_agent(id)` | — | Idempotent |
| `get_run(agent_id, run_id)` | Run | — |
| `wait(agent_id, run_id?, timeout_s?)` | Run | Polls until terminal |
| `watch(agent_id, run_id, read_timeout_s?)` | Iterator[SseEvent] | Streams events; auto-reconnect |
| `cancel(agent_id, run_id)` | — | SIGTERM + grace |
| `usage(agent_id)` | Usage | Per-run costs |
| `models()` | list[Model] | Supported models |
| `me()` | Account | Current API key info |
| `create_artifact(agent_id, run_id?, test_command?)` | Artifact | — |
| `artifacts.list(agent_id, run_id?)` | list[Artifact] | — |
| `artifacts.download(agent_id, artifact_id, dest?)` | bytes | Download to file or memory |
| `recover(workflow_id)` | Workflow | Fetch agents + runs + artifacts |
| `close_workflow(id)` | — | Idempotent cleanup |

## Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `SBX_BASE_URL` | `https://sbx.sorenforge.com` | Control plane endpoint |
| `SBX_API_KEY` | (required) | Bearer token (scopes: `agents`, `admin`) |
| `SBX_HTTP_TIMEOUT_S` | — | Override all timeouts (connect/read/write/pool) |
