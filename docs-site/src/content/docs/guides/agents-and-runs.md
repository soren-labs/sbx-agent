---
title: Agents and runs
description: Create agents, queue runs, stream events, and track usage.
---

## Creating an agent

Create an agent and queue its first run:

```bash
POST /v1/agents
Content-Type: application/json
Authorization: Bearer sbx_<key>

{
  "prompt": {"text": "Write a function that validates emails"},
  "agent": {
    "provider": "codex",
    "account_id": "auto"
  }
}
```

**Response: 201 Created**

```json
{
  "agent": {
    "id": "agent_abc123...",
    "name": null,
    "provider": "codex",
    "account_id": "codex-1",
    "model": null,
    "reasoning_effort": null,
    "status": "running",
    "created_at": "2026-09-23T10:15:00Z",
    "updated_at": "2026-09-23T10:15:00Z"
  },
  "run": {
    "id": "run_xyz789...",
    "agent_id": "agent_abc123...",
    "status": "CREATING",
    "created_at": "2026-09-23T10:15:00Z",
    "updated_at": "2026-09-23T10:15:00Z",
    "started_at": null,
    "finished_at": null,
    "result": null,
    "error": null
  }
}
```

The endpoint returns 201 immediately with an agent and first run in initialized state. Both may still be starting. Poll or stream to watch progress.

### Request body

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `prompt` | object | ✅ | Container with `text` field |
| `prompt.text` | string | ✅ | Agent instruction |
| `agent` | object | ✅ | Provider configuration |
| `agent.provider` | string | ✅ | `codex`, `devin`, `antigravity`, `grok`, `opencode` |
| `agent.account_id` | string | ✅ | Account ID or `"auto"` |
| `agent.model` | string | ❌ | Model ID; defaults to provider's default |
| `agent.reasoning_effort` | string | ❌ | `low`, `medium`, `high` |
| `name` | string | ❌ | Display name |
| `idle_timeout_s` | integer | ❌ | Idle timeout in seconds |
| `metadata` | object | ❌ | Workflow context (`workflow_id`, `task_id`, `role`, `parent_task_id`) |
| `workspace` | object | ❌ | Git repo (`repo`, `base_ref`, `base_sha`) |
| `handoff` | object | ❌ | Start from artifact/commit (`artifact_id` or `head_sha`) |
| `git` | object | ❌ | Git push/PR (`branch`, `push`, `auto_create_pr`, `target`, `draft`) |
| `output_contract` | object | ❌ | JSON Schema contract for output |
| `output_contract.schema` | object | ✅ | JSON Schema (subset; see [Structured Output](/guides/structured-output/)) |
| `output_contract.enforcement` | string | ❌ | `strict` (default) or `warn` |
| `resources` | object | ❌ | Optional resources (`secrets`, `mcp`) |
| `compute` | object | ❌ | Sandbox sizing (`cpu`, `memory_mib`) |

### Validation errors

**Unknown provider or malformed body:**

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json

{
  "error": {
    "code": "invalid_provider",
    "message": "malformed request"
  }
}
```

**Scheduler at capacity:**

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json

{
  "error": {
    "code": "concurrency_limit",
    "message": "global live-agent cap (SBX_MAX_CONCURRENT) reached — idle agents hold slots until closed",
    "retry_after": 60
  }
}
```

## Getting an agent and its runs

```bash
GET /v1/agents/{id}
GET /v1/agents/{id}/runs
GET /v1/agents/{id}/runs/{runId}
```

Returns current agent status, all runs, or a specific run with status, error, and usage.

## Follow-up runs (multi-turn)

Resume the provider session on an idle agent:

```bash
POST /v1/agents/{id}/runs
Content-Type: application/json
Authorization: Bearer sbx_<key>

{
  "prompt": {"text": "Now add email verification via a POST endpoint"}
}
```

**Response: 201 Created**

Returns a new run on the same agent, reusing the sandbox and provider session. Only `prompt` is required; other fields (like `output_contract`) override per-run.

**Errors:**

If agent is not idle:

```http
HTTP/1.1 409 Conflict
Content-Type: application/json

{
  "error": {
    "code": "turn_in_progress",
    "message": "a run is in progress"
  }
}
```

If agent is closed or timed out:

```http
HTTP/1.1 409 Conflict
Content-Type: application/json

{
  "error": {
    "code": "session_not_runnable",
    "message": "agent status is closed"
  }
}
```

## Cancelling a run

```bash
POST /v1/agents/{id}/runs/{runId}/cancel
Authorization: Bearer sbx_<key>
```

Sends SIGTERM to the provider (grace ~30s, then SIGKILL). Run persists as `CANCELLED`. Idempotent.

## Closing an agent

```bash
DELETE /v1/agents/{id}
Authorization: Bearer sbx_<key>
```

Closes the agent, reclaims the sandbox, makes run history read-only. Idempotent.

## Idempotency

Create agents idempotently with the `Idempotency-Key` header:

```bash
curl -X POST $SBX_BASE_URL/v1/agents \
  -H "Authorization: Bearer sbx_<key>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: unique-request-id" \
  -d '{"prompt": {"text": "..."}, "agent": {...}}'
```

Replaying the same key within the idempotency window returns the original response. Retrying with a different body returns `409 idempotency_conflict`. Follow-up runs do not support idempotency keys.

## Usage and cost estimation

```bash
GET /v1/agents/{id}/usage
Authorization: Bearer sbx_<key>
```

**Response:**

```json
{
  "runs": [
    {
      "id": "run_xyz789...",
      "status": "FINISHED",
      "usage": {
        "input_tokens": 4200,
        "cached_input_tokens": 2000,
        "output_tokens": 1850,
        "cache_write_input_tokens": 500,
        "reasoning_output_tokens": 0
      },
      "cost_estimate_usd": 0.315
    }
  ],
  "total_cost_estimate_usd": 0.315
}
```

`cost_estimate_usd` is a Modal list-price estimate. Token counts come from the provider.

## Python client example

```python
from examples.sbx_client import SbxClient

client = SbxClient(api_key="sbx_...", base_url="$SBX_BASE_URL")

# Create agent
result = client.create_agent(
    text="Write a function that validates emails", provider="codex", account_id="auto"
)
agent_id = result["agent"]["id"]
run_id = result["run"]["id"]

# Watch events
for event in client.watch(agent_id, run_id):
    print(f"{event.type}: {event.data}")
    if event.type == "sbx.turn_finished":
        break

# Or poll for terminal state
run = client.wait(agent_id, run_id)
print(f"Status: {run['status']}")

# Follow-up
followup = client.followup(agent_id, text="Add POST verification")

# Usage
usage = client.usage(agent_id)
print(f"Cost: ${usage['total_cost_estimate_usd']:.2f}")

# Clean up
client.close_agent(agent_id)
```

## curl example

```bash
# Create agent
RESULT=$(curl -s -X POST $SBX_BASE_URL/v1/agents \
  -H "Authorization: Bearer sbx_<key>" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": {"text": "Write a validation function"},
    "agent": {"provider": "codex", "account_id": "auto"}
  }')

AGENT_ID=$(echo "$RESULT" | jq -r '.agent.id')
RUN_ID=$(echo "$RESULT" | jq -r '.run.id')

# Stream events
curl -N \
  "$SBX_BASE_URL/v1/agents/$AGENT_ID/runs/$RUN_ID/stream" \
  -H "Authorization: Bearer sbx_<key>"

# Poll status
curl -s $SBX_BASE_URL/v1/agents/$AGENT_ID/runs/$RUN_ID \
  -H "Authorization: Bearer sbx_<key>" | jq '.status'

# Usage
curl -s $SBX_BASE_URL/v1/agents/$AGENT_ID/usage \
  -H "Authorization: Bearer sbx_<key>" | jq '.total_cost_estimate_usd'

# Cancel run
curl -X POST \
  $SBX_BASE_URL/v1/agents/$AGENT_ID/runs/$RUN_ID/cancel \
  -H "Authorization: Bearer sbx_<key>"
```

## Error reference

See [Errors](/reference/errors/) for the complete catalog. Common cases:

- `rate_limited`, `quota_exhausted`, `model_capacity`, `timeout`, `runtime_error`: retry with backoff
- `auth_invalid`: fix credentials
- `model_unavailable`: use a different model
- `contract_violation`: output failed schema validation (strict mode only)

```json
{
  "status": "ERROR",
  "error": {
    "code": "auth_invalid",
    "source": "provider",
    "message": "missing or invalid bearer token",
    "retryable": false
  }
}
```
