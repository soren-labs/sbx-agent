---
title: Workflows
description: Bind agents to a workflow id so any process holding the same API key can recover the whole group or clean it up in one call.
---

An orchestrator that drives several agents usually keeps their ids in memory.
If it crashes, those ids are gone. A **workflow** fixes that: tag each agent
with a `workflow_id`, and a fresh process that has only the API key and the
workflow id can find every agent, its latest run and its artifacts again — or
close them all.

Workflows are scoped to the API key that created the agents. Two keys can use
the same workflow id without ever seeing each other's agents.

## Bind an agent

Send `metadata` when you create the agent:

| Field | Required | Meaning |
| --- | --- | --- |
| `workflow_id` | yes | Your identifier for the workflow (1–256 characters). |
| `task_id` | yes | The task this agent works on inside the workflow (1–256 characters). |
| `role` | yes | A free-form label such as `worker` or `reviewer` (1–64 characters). |
| `parent_task_id` | no | Links a sub-task to its parent task. |

```bash
curl -X POST "$SBX_BASE_URL/v1/agents" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": {"text": "Draft the migration plan"},
    "agent": {"provider": "codex"},
    "metadata": {"workflow_id": "release-42", "task_id": "plan", "role": "planner"}
  }'
```

The binding is echoed as `metadata` on the agent. A follow-up run
(`POST /v1/agents/{id}/runs`) may also carry `metadata`, which re-binds the
agent to that task.

## Recover a workflow

`GET /v1/workflows/{id}` returns everything a new process needs to resume
polling or streaming. It is served from durable records only — no sandbox is
contacted — so it is cheap enough to poll.

```json
{
  "workflow_id": "release-42",
  "agents": [
    {
      "agent_id": "61833ea65ae9497a8f502d2b8bcf414d",
      "task_id": "plan",
      "role": "planner",
      "parent_task_id": null,
      "attached_at": "2026-09-23T01:09:43+00:00",
      "status": "idle",
      "provider": "codex",
      "account_id": "codex-1",
      "model": "gpt-5.6-luna",
      "agent_created_at": "2026-09-23T01:09:43+00:00",
      "runs": 1,
      "latest_run": { "id": "run-1", "status": "FINISHED" }
    }
  ],
  "progress": {
    "tasks": 1,
    "agents": 1,
    "open_agents": 1,
    "runs": 1,
    "latest_runs_by_status": { "FINISHED": 1 },
    "all_terminal": true
  }
}
```

`latest_run` is a full run record (trimmed above), including its `artifact_refs`.
`progress.open_agents` counts agents that are not closed, timed out or lost;
`all_terminal` is true once every agent's latest run has reached a terminal
status. An unknown workflow id returns `404 not_found`.

You can also list the bound agents with `GET /v1/agents?workflow_id=release-42`.

## Clean up a workflow

`DELETE /v1/workflows/{id}` closes exactly the agents bound to this workflow
under your key. Nothing else is touched: the owner of every agent is checked
again before it is closed.

```json
{
  "workflow_id": "release-42",
  "matched": 3,
  "closed": ["61833ea65ae9497a8f502d2b8bcf414d"],
  "already_terminal": ["ad73a535ecc344e2bc0f4c7d1a9b8e6f"],
  "missing": [],
  "skipped": [],
  "errors": {}
}
```

The call is idempotent: repeating it reports the agents under
`already_terminal` instead of closing them again. Closing an agent works like
`DELETE /v1/agents/{id}` — the sandbox goes away, run history and artifacts
stay readable.

## With the Python client

`client.recover(workflow_id)` rebuilds the picture client-side from
`GET /v1/agents?workflow_id=…` and each agent's run list. It returns a
`WorkflowRecovery`:

| Attribute | Contents |
| --- | --- |
| `workflow_id` | The id you asked for. |
| `agents` | Agent records whose `metadata.workflow_id` matches. |
| `runs` | `{agent_id: [run, …]}`. |
| `latest_runs` | `{agent_id: latest run}`. |
| `handles(latest_only=True)` | `[(agent_id, run_id), …]`, ready for `wait_many` or `watch`. |
| `artifact_refs()` | `{agent_id: ["artifact://…", …]}` from each agent's latest run. |

`client.close_workflow(workflow_id)` closes every recovered agent with
`DELETE /v1/agents/{id}` and returns the closed records.

```python
from sbx.sdk import SbxClient

WORKFLOW = "release-42"

with SbxClient() as client:
    for task, prompt in [
        ("api", "Implement the /v2/orders endpoint"),
        ("docs", "Document /v2/orders"),
    ]:
        client.create(
            prompt,
            provider="codex",
            metadata={"workflow_id": WORKFLOW, "task_id": task, "role": "worker"},
        )

# ...the orchestrator restarts and has nothing but the key and the workflow id...

with SbxClient() as client:
    recovery = client.recover(WORKFLOW)
    finished = client.wait_many(recovery.handles(), timeout_s=1800)
    for (agent_id, run_id), run in finished.items():
        print(agent_id, run_id, run["status"])
    client.close_workflow(WORKFLOW)
```

The console's **Workflows** page shows the same recovery view and has a
**Close workflow** button — see [Web console](/guides/console/#workflows).
