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

`SbxClient()` reads `SBX_BASE_URL` and `SBX_API_KEY` from the environment. The built-in default for `SBX_BASE_URL` is the maintainers' deployment (`https://sbx.sorenforge.com`); self-hosters must set `SBX_BASE_URL` to their own control plane. You can also pass `base_url=` / `api_key=` (or an injected `httpx.Client` via `client=`) explicitly.

**Everything returns plain dicts.** The client is a thin layer over the `/v1` JSON contract — methods return the decoded response body, never typed objects. There are no `Agent`/`Run`/`Usage` classes; you read fields with `payload["id"]`, `payload["status"]`, etc. The only objects are `SseEvent`, `WorkflowRecovery`, and the two error types.

## Creating agents

```python
created = client.create_agent(
    text="Write a function that validates emails",
    provider="codex",
    model="gpt-5-turbo",
    account_id="auto",           # default
    name=None,                   # optional display name
    idle_timeout_s=None,         # optional
    metadata=None,               # optional: workflow_id / task_id / role / parent_task_id
    workspace=None,              # optional: {"repo", "base_ref", "base_sha"}
    handoff=None,                # optional: {"artifact_id": ...} or {"head_sha": ...}
    output_contract=None,        # optional: {"schema": <JSON Schema>, "enforcement": "strict"|"warn"}
    idempotency_key=None,        # optional: Idempotency-Key header
)
agent, run = created["agent"], created["run"]
print(agent["id"], agent["status"], run["id"], run["status"])
```

`create_agent` returns the `POST /v1/agents` body: `{"agent": {...}, "run": {...}}`. The first run may still be `CREATING` — poll `wait` for the terminal state. `client.create(...)` is an alias for `create_agent`.

## Follow-up runs

```python
run = client.followup(
    agent["id"],
    text="Now add async support",
    # metadata=..., output_contract=... are optional per-run overrides
)
print(run["id"], run["status"])
```

`followup` returns the new run dict. `client.create_run(agent_id, text, ...)` is an alias.

## Waiting for completion

```python
run = client.wait(agent["id"], run["id"], timeout_s=300, poll_s=2.0)
# run["status"] is terminal: FINISHED, ERROR, CANCELLED, EXPIRED, or UNKNOWN
print(f"Status: {run['status']}")
if run.get("error"):
    print(f"Error: {run['error']['code']} ({run['error']['source']})")
```

`run_id` is a required positional — there is no "latest run" default. Use `resume` (below) or `list_runs(agent_id)[-1]` to pick the latest. `wait` polls `GET .../runs/{runId}` until a persisted terminal status or `timeout_s`, and returns the last observed run payload (a timeout returns the status as observed, never inferred). `wait_many(runs, timeout_s=..., poll_s=...)` waits on many runs under one budget and returns `{(agent_id, run_id): run}`.

## Watching events

```python
for event in client.watch(agent["id"], run["id"], read_timeout_s=120):
    print(f"{event.type}: {event.data}")
    if event.type == "sbx.turn_finished":
        break
```

Yields `SseEvent` objects with `.id`, `.type`, `.data`. Automatically reconnects with `Last-Event-ID` (bounded by `max_reconnects`), then a persisted-terminal GET fallback. Interrupting `watch` only detaches the local stream — it never cancels remote work. `stream_run(agent_id, run_id, last_event_id=None, read_timeout_s=90)` is the single-pass variant with no reconnect.

## Resuming after a restart

```python
for event in client.resume(agent["id"], run_id=None, last_event_id=None):
    ...
```

`resume` re-attaches to a run's event stream. `run_id=None` selects the agent's latest run — this is the "omit for latest" behavior.

## Cancelling

```python
cancelled = client.cancel(agent["id"], run["id"])
print(cancelled["status"])
```

## Closing agents

```python
closed = client.close_agent(agent["id"])
```

## Usage & costs

```python
usage = client.usage(agent["id"])
print(f"Total: {usage['cost_estimate_usd']} USD, {usage['sandbox_seconds']}s sandbox")
if usage["usage"] is not None:
    print(usage["usage"]["input_tokens"], usage["usage"]["output_tokens"])
```

Returns `{"usage": {...} | None, "cost_estimate_usd": float, "sandbox_seconds": float}`. `usage["usage"]` is `None` while no usage was ever measured — unavailable, never fabricated zeros.

## Artifacts

```python
artifact = client.create_artifact(
    agent["id"],
    run_id=None,                 # optional; default: the agent's latest run
    test_command="npm test",     # optional
)
print(artifact["artifact_id"])

artifacts = client.artifacts.list(agent_id=agent["id"], run_id=None)
data = client.artifacts.download(agent["id"], artifact["artifact_id"], "artifact.zip")
```

`create_artifact` returns the artifact manifest dict. `artifacts.list` returns `list[dict]` (`run_id` filters client-side on `producer.run_id`). `artifacts.download` returns the `patch.diff` bytes and optionally writes them to `dest`. Top-level helpers `list_artifacts`, `get_artifact`, and `download_artifact(artifact_id, member="patch.diff")` do the same without going through the `artifacts` seam.

## Workflow recovery

```python
workflow = client.recover(workflow_id="my-task-2026-09-23")
for agent in workflow.agents:
    print(f"Agent {agent['id']}: {agent['status']}")

finished = client.wait_many(workflow.handles(), timeout_s=1800)
refs = workflow.artifact_refs()  # {agent_id: ["artifact://...", ...]}
client.close_workflow(workflow_id)
```

`recover` returns a `WorkflowRecovery` with `.agents` (`list[dict]`), `.runs` (`{agent_id: [run, ...]}`), `.latest_runs`, `.handles(latest_only=True)` (run pairs for `wait_many`/`resume`), and `.artifact_refs()`.

## Error handling

```python
from examples.sbx_client import SbxApiError, SbxTransportError

try:
    created = client.create_agent(text="...")
except SbxApiError as e:
    print(f"API error: {e.status} {e.code} — {e}")
    if e.retry_after:
        print(f"Retry after: {e.retry_after}s")
except SbxTransportError as e:
    print(f"Transport error: {e.method} {e.path}")
    if e.check:
        print(f"Run this to verify: {e.check}")
    if e.idempotent:
        print("Safe to retry (idempotent)")
```

`SbxApiError` carries `.status`, `.code`, and `.retry_after`; the canonical error `message` is the exception text (`str(e)`), not a `.message` attribute. `SbxTransportError` carries `.method`, `.path`, `.check` (the durable GET to run before retrying), `.idempotent`, and `.original` (the httpx failure).

## Methods (full reference)

| Method | Returns | Notes |
| --- | --- | --- |
| `create_agent(text, provider, account_id, model, name, *, ...)` | `{"agent", "run"}` | Starts agent + run 1; `create` is an alias |
| `followup(agent_id, text, *, metadata?, output_contract?)` | run dict | New run on idle agent; `create_run` is an alias |
| `list_agents(**params)` | `{"agents", "next_cursor"}` | e.g. `workflow_id=`, `cursor=` |
| `get_agent(agent_id)` | agent dict | — |
| `close_agent(agent_id)` / `delete_agent(agent_id)` | agent dict | Idempotent |
| `list_runs(agent_id)` | `list[run]` | — |
| `get_run(agent_id, run_id)` | run dict | — |
| `wait(agent_id, run_id, *, timeout_s, poll_s)` | run dict | Polls until terminal; `run_id` required |
| `wait_many(runs, *, timeout_s, poll_s)` | `{(agent_id, run_id): run}` | One shared timeout budget |
| `watch(agent_id, run_id, *, last_event_id?, max_reconnects?, ...)` | `Iterator[SseEvent]` | Streams events; auto-reconnect |
| `stream_run(agent_id, run_id, last_event_id?, *, read_timeout_s?)` | `Iterator[SseEvent]` | Single pass, no reconnect |
| `resume(agent_id, run_id?, *, last_event_id?, ...)` | `Iterator[SseEvent]` | `run_id=None` → latest run |
| `cancel(agent_id, run_id)` / `cancel_run(...)` | run dict | SIGTERM + grace |
| `usage(agent_id)` | `{"usage", "cost_estimate_usd", "sandbox_seconds"}` | Aggregate for the agent |
| `models()` | `list[dict]` | Supported models per provider |
| `me()` | dict | Current API key info |
| `get_workspace(agent_id)` | workspace dict | — |
| `review_workspace(agent_id, head_sha?)` | workspace dict | Idempotent review pin |
| `apply_handoff(agent_id, *, artifact_id?, head_sha?, workspace?)` | dict | Handoff into an existing agent |
| `create_artifact(agent_id, run_id?, test_command?)` | artifact dict | — |
| `artifacts.list(agent_id, run_id?)` | `list[dict]` | — |
| `artifacts.download(agent_id, artifact_id, dest?)` | bytes | `patch.diff`; writes `dest` if given |
| `list_artifacts(agent_id?)` / `get_artifact(id)` | `list[dict]` / dict | Top-level variants |
| `download_artifact(artifact_id, member?)` | bytes | Any member (`patch.diff` default) |
| `recover(workflow_id)` | `WorkflowRecovery` | Fetch agents + runs + artifact refs |
| `close_workflow(workflow_id)` | `list[dict]` | Idempotent cleanup |
| `close()` | — | Close the HTTP client (context manager supported) |

## Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `SBX_BASE_URL` | `https://sbx.sorenforge.com` | Control plane endpoint |
| `SBX_API_KEY` | (required) | Bearer token (scopes: `agents`, `admin`) |
| `SBX_HTTP_TIMEOUT_S` | — | Override all timeouts (connect/read/write/pool) |
