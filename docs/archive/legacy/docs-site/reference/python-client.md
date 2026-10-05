---
title: Python client
description: The sbx.sdk client for the /v1 API — task-first, typed models, safe retries and durable reads.
---

`sbx.sdk.SbxClient` is a Python wrapper around the `/v1` API. It focuses on
[tasks](/guides/tasks/) first and exposes the agent/run surface underneath
for lower-level work.

Install it with the `sbx` package (`pip install -e .` from the repo, or the
published package) — it ships with the CLI.

```python
from sbx.sdk import SbxClient

client = SbxClient()  # reads SBX_BASE_URL and SBX_API_KEY
```

| Constructor arg | Env fallback | Default |
| --- | --- | --- |
| `base_url` | `SBX_BASE_URL` | `https://sbx.sorenforge.com` |
| `api_key` | `SBX_API_KEY` | required |
| `client` | — | an internal `httpx.Client` |
| `timeout` | `SBX_HTTP_TIMEOUT_S` | `httpx.Timeout(30.0, read=10.0)` |
| `auto_idempotency` | — | `True` — every mutating call gets an `Idempotency-Key` |

The client is not thread-safe; make one per worker. Call `client.close()`
(or use it as a context manager) to release the transport.

## Tasks

`client.tasks` covers the whole [task lifecycle](/guides/tasks/):

```python
created = client.tasks.create(
    "Fix the flaky date test",  # prompt text
    name="nightly-flake",
    source={"repo": "https://github.com/acme/api"},  # ref pins/branches too
    execution={"provider": "codex"},  # auto when omitted
    delivery={"pull_request": {"title": "Fix flaky date test"}},
)
task_id = created.task.id  # also created.agent / created.run

# Same request, side-effect free — validate before committing
check = client.tasks.preflight(
    "Fix the flaky date test", source={"repo": "https://github.com/acme/api"}
)

detail = client.tasks.get(task_id)  # task + agent + runs + delivery
tasks = client.tasks.list()  # ?status=…&agent_id=…&limit=…
run = client.tasks.followup(task_id, "Also add a regression test", on_busy="queue")  # queue|reject
task = client.tasks.wait(task_id)  # polls to a terminal state
runs = client.tasks.runs(task_id)  # every run on the task

client.tasks.cancel(task_id)  # idempotent
client.tasks.retry(task_id)  # mode="run" | "delivery" | auto
```

Delivery, review and merge:

```python
detail = client.tasks.delivery(task_id)  # run the declared policy now
revision = client.tasks.deliver(task_id)  # materialize + push + open PR
client.tasks.deliver(task_id, revision="rev-000001", pull_request={"title": "…", "draft": True})
revisions = client.tasks.revisions(task_id)
revision = client.tasks.revision(task_id)  # latest
review = client.tasks.review(task_id, verdict="approve", reviewer={"identity": "ci"})
merged = client.tasks.merge(task_id)  # gated — see below
```

`create`, `preflight` and `run` accept the `CreateTaskRequest` fields as
keyword arguments — see [Creating a task](/guides/tasks/#creating-a-task)
for the full table.

`tasks.merge` requires a **non-stale independent approve** on the revision's
exact head — the same gate described in
[Repositories, revisions and delivery](/guides/repositories/).

## Agents and runs

The lower-level surface a task resolves onto:

```python
result = client.agents.create("Write a validation function", provider="codex", account_id="auto")
agent_id = result["agent"]["id"]
run_id = result["run"]["id"]

run = client.runs.wait(agent_id, run_id)
for event in client.runs.watch(agent_id, run_id):
    print(event.type, event.data)

follow = client.runs.create(agent_id, "Add tests")
client.runs.cancel(agent_id, run_id)
client.agents.close(agent_id)  # idempotent close
```

`client.agents` mirrors `POST|GET|DELETE /v1/agents` plus
`GET /v1/agents/{id}/usage`, `…/workspace`, `…/workspace/review`,
`…/handoff` and `…/revisions`. `client.runs` mirrors
`POST|GET /v1/agents/{id}/runs`, `…/runs/{runId}` and
`…/runs/{runId}/cancel`. `client.artifacts` covers `GET|POST /v1/artifacts`
and downloads (`download` writes `patch.diff` or another member to disk).
`client.recover(workflow_id)` / `client.close_workflow(workflow_id)` handle
workflow recovery.

For a task's follow-up run prefer `client.tasks.followup(...)` — it keeps
the task's run list coherent.

## Streaming and resuming

`watch` / `stream` return an iterator of `SseEvent` (`event.id`,
`event.type`, `event.data`). Streams are run-scoped — a task's events come
from its agent's runs:

```python
for event in client.watch(created.agent.id, created.run.id):
    if event.type == "sbx.run_finished":
        break
```

After a client-side interruption, `client.resume(agent_id)` reattaches at
the last acknowledged event — the server deduplicates on `Last-Event-ID`.
The same applies to `client.runs.resume(agent_id)` and
`client.runs.watch(agent_id, run_id)`.

`client.wait_many([...])` waits on a batch of runs and returns the
finished (or last-polled) `Run`s keyed by `(agent_id, run_id)` — pass a
per-request timeout and poll budget via `timeout_s` / `poll_s`.

## Typed models

Every call returns a typed wrapper — `Task`, `TaskDetail`, `TaskCreated`, `Run`, `Revision`,
`Delivery`, `Review`, `RunError`, `WorkflowRecovery`, `SseEvent` — with
attribute access on the fields
the API guarantees (`task.status`, `run.id`, `revision.head_sha`,
…) plus `.raw` for the full decoded payload.
Unguaranteed optional fields return `None` where documented or raise
`AttributeError`; everything else is reachable via `.raw`.

`tasks.get` returns `TaskDetail` (`resolved`, `latest` revision,
`delivery`); `tasks.list`/`revisions`/`reviews` return lists of the item
types.

## Retries and errors

- **Automatic idempotency** — every mutating call sends an
  `Idempotency-Key`. Replays return the stored response with
  `Idempotent-Replay: true`; retrying a create with a different body raises
  `409 idempotency_conflict`.
- **`SbxApiError`** — any `4xx`/`5xx` response, with `status`, `code` (one
  of the catalog subcodes), `retryable`, `action`, `retry_after` and
  `details`.
- **`SbxTransportError`** — a bounded request timed out or dropped at the
  transport layer. `check` names the durable `GET` to run before retrying
  (a timed-out `POST` may already be persisted); `idempotent` marks calls a
  blind retry cannot double-apply.

```python
from sbx.sdk import SbxClient, SbxApiError, SbxTransportError

client = SbxClient()
try:
    created = client.tasks.create("Fix the flaky date test")
except SbxApiError as e:
    print(e.status, e.code, e.retryable, e.retry_after)
except SbxTransportError as e:
    print(e.check)  # durable read to run before retrying
```

`client.http` is the raw `httpx.Client` for endpoints without a convenience
method; prefer the typed surface — it keeps error and idempotency behavior
consistent.

## CLI equivalent

```bash
sbx status                              # key / account / capacity check
sbx smoke --provider codex --prompt "Fix the flaky date test"
```

The `sbx` CLI manages deployment, auth and diagnostics — task submission is
an API/SDK operation. See the [CLI reference](/reference/cli/).
