---
title: Tasks
description: The caller-facing unit of work — create a task, let the control plane resolve the agent, and track it through delivery.
---

A **task** is the primary way to run work: you describe the goal and any
constraints (a repository, a provider preference, a pull-request policy), and
the control plane resolves the rest — which repository ref to start from,
which provider account and model to use, and where the result goes.

Tasks sit on top of the lower-level [agents and runs](/guides/agents-and-runs/)
surface. A task owns one agent; every prompt you send it is a run on that
agent, resuming the provider's native session. You rarely need to touch the
agent yourself — the task record carries it.

## Try it without committing

`POST /v1/tasks/preflight` evaluates a request against live state — repository
access, ref resolution, account capacity — and returns the resolution it
*would* use, with per-field evidence. Nothing is reserved and nothing runs.

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks/preflight" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": {"text": "Fix the flaky date test"},
    "source": {"repo": "https://github.com/acme/api"},
    "execution": {"provider": "codex"}
  }'
```

The console's **New task** form runs the same checks as you fill it in.

## Create a task

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": {"text": "Fix the flaky date test"},
    "name": "fix-date-test",
    "source": {"repo": "https://github.com/acme/api"},
    "delivery": {
      "pull_request": {"title": "Fix flaky date test", "draft": true}
    }
  }'
```

**Response: 201 Created**

```json
{
  "task": {
    "id": "task_2f8c…",
    "status": "queued",
    "prompt": "Fix the flaky date test",
    "agent_id": "agent_abc123…",
    "run_id": "run_xyz789…"
  },
  "agent": {"id": "agent_abc123…", "provider": "codex", "status": "creating"},
  "run": {"id": "run_xyz789…", "status": "CREATING"}
}
```

### Request fields

| Field | Required | Meaning |
| --- | --- | --- |
| `prompt` | ✅ | `{ "text": "…" }` — the goal, in plain language. |
| `name` | — | Display name; the console generates a short title from the prompt when omitted. |
| `source` | — | `{repo, ref}` — what to work on. `repo` is any git URL the sandbox can reach (github.com URLs are canonicalized). `ref` defaults to the repo's default branch; a branch/tag resolves to its sha, a 40-hex sha pins the commit. Leave empty for a plain sandbox task. |
| `execution` | — | `{provider, account_id, model, reasoning_effort}` — each accepts an explicit value or `auto` (the default): the scheduler picks among eligible accounts and discovered models. |
| `delivery` | — | `{branch, pull_request, auto_publish}` — where the result goes. `branch` names the work branch; `pull_request` takes `{title, body, draft, target}`; `auto_publish` pushes on every finished run. See [Repositories and delivery](/guides/repositories/). |
| `metadata` | — | `{workflow_id, task_id, role, parent_task_id}` — binds the task's agent to a [workflow](/guides/workflows/). |
| `output_contract` | — | `{schema, enforcement}` — a JSON Schema the result must satisfy ([structured output](/guides/structured-output/)). |
| `resources` | — | `{secrets, mcp}` — allow-listed Modal Secrets and MCP servers ([resources](/guides/resources-and-compute/)). |
| `compute` | — | `{cpu, memory_mib}` — sandbox sizing as `[request, limit]` pairs ([limits](/reference/limits/)). |
| `idle_timeout_s` | — | How long the agent stays warm for follow-ups. |

The response echoes `resolved`: what the control plane actually chose
(canonical repo, base ref, exact base sha, provider, account, model, effort)
and, per field, whether it came from your request, the account, the LRU
schedule, model discovery, or a default.

## Watch a task

```bash
curl "$SBX_BASE_URL/v1/tasks/$TASK_ID" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

`GET /v1/tasks/{id}` returns the task record plus live `agent` and `run`
views. Poll it, or open the task in the [console](/guides/console/) — the
console shows the same status, the live run conversation, and a **Publish**
step when the task produces code.

### Task statuses

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for a slot or dispatching (`awaiting_dispatch` inside) |
| `running` | A run is in progress on the task's agent |
| `delivering` | The run finished; the declared delivery (push/PR) is still in flight |
| `finished` | Done, including any required delivery |
| `delivery_failed` | The run finished but a required delivery failed — [retry](/guides/tasks/#retry) publishes it without re-running |
| `error` | The latest run failed — see the run's structured `error` |
| `expired` | The agent hit a lifecycle timeout |
| `cancelled` | Cancelled by `POST /v1/tasks/{id}/cancel` |

Statuses are computed from the durable run ledger and workspace record, so a
terminal status never regresses. A run that `FINISHED` but whose required
delivery failed reports `delivery_failed`, not `finished`.

### Watch the run itself

Each task run streams canonical events over SSE — the same stream the console
renders as a conversation:

```bash
curl -N "$SBX_BASE_URL/v1/agents/$AGENT_ID/runs/$RUN_ID/stream" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

The task detail's `run.id` names the latest run; `GET /v1/tasks/{id}/runs`
lists them all. See [Streaming](/guides/streaming/) for `Last-Event-ID`
resume and the polling fallback.

## Follow up

Send another instruction on the same task — it queues a run on the same
agent, which keeps its sandbox and resumes the provider's native session:

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks/$TASK_ID/runs" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": {"text": "Now add a regression test"}}'
```

Follow-ups **queue** while a run is in flight (`on_busy: "queue"`, the
default). Pass `"on_busy": "reject"` to get a `409 task_active` instead. Each
follow-up may carry its own `metadata` and `output_contract`.

## Cancel

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks/$TASK_ID/cancel" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

Cancels the active run and drops queued work. Anything already published
stays published. Cancel is idempotent — repeating it on a terminal task
replays the current state.

## Retry

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks/$TASK_ID/retry" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"mode": "delivery"}'
```

Retry is lane-aware:

- `"delivery"` — republish the existing work: rerun the declared delivery
  policy against the recorded head. Use this for `delivery_failed`.
- `"run"` — queue a fresh run on the same agent (optionally with a new
  `prompt`), e.g. after `error` or `expired`.

A task that never reached a retryable state returns `409
task_not_retryable`. For mid-flight work use [cancel](#cancel) or a
[follow-up](#follow-up) instead.

## Idempotency

Every task mutation honors `Idempotency-Key`: replaying the same key returns
the original result instead of creating a second task. The Python SDK sends a
fresh key automatically, so a transport-failed `tasks.create` retries safely
— pass `idempotency_key=` to pin your own, or `False` to opt out.

## With the Python SDK

```python
from sbx.sdk import SbxClient

client = SbxClient()  # SBX_BASE_URL + SBX_API_KEY

created = client.tasks.create(
    "Fix the flaky date test",
    source={"repo": "https://github.com/acme/api"},
    delivery={"pull_request": {"title": "Fix flaky date test"}},
)
task = client.tasks.wait(created.task.id)  # polls until a terminal status
print(task.status)

run = client.tasks.followup(created.task.id, "Now add a regression test")
detail = client.tasks.get(created.task.id)  # task + agent + run views
```

`tasks.create` returns a `TaskCreated` (`task` + `agent` + `run`);
`tasks.wait` returns the last observed `Task` — check `task.status` rather
than assuming success. The full method list is in the
[Python SDK reference](/reference/python-client/).

## Errors

Task routes return the canonical error shape
(`{"error": {"code", "message", "retryable", "action", …}}`). The common
ones:

| Code | Status | Meaning |
| --- | --- | --- |
| `invalid_source` | 400 | The repository URL or ref can't be resolved. |
| `invalid_provider` | 400 | Unknown provider, or provider not enabled on this deployment. |
| `provider_exhausted` | 409/429 | No eligible account has a free slot — `retry_after` hints when. |
| `concurrency_limit` | 429 | The live-agent cap is reached; close idle agents or wait. |
| `task_active` | 409 | A run is already in flight and `on_busy` was `reject`. |
| `task_not_retryable` | 409 | Retry was requested on a task that has nothing to retry. |
| `not_found` | 404 | Unknown task id, or a task owned by a different API key. |

Full catalog: [error reference](/reference/errors/).
