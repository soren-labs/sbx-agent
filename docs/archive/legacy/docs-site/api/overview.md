---
title: API overview
description: The public /v1 contract, authentication, errors and task-oriented resource model.
---

SBX exposes one public REST surface under `/v1`. It is the contract used by
the web console and the Python SDK.

## Base URL

The operator gives you a control-plane origin such as:

```text
https://sbx.example.com
```

Every public endpoint is relative to that origin. The canonical machine
contract is available at:

```text
GET /v1/openapi.json
```

The docs site also publishes the build-time copy at [/openapi.json](/openapi.json).

## Authentication

Send the API key on every request:

```http
Authorization: Bearer sbx_...
```

See [Authentication](/api/authentication/) for scopes and key lifecycle.

## Main task resources

| Resource | Purpose |
| --- | --- |
| `/v1/tasks` | Create/list task intent and read computed product state. |
| `/v1/tasks/{id}/runs` | Queue and list follow-up runs. |
| `/v1/tasks/{id}/revisions` | Read durable code results. |
| `/v1/tasks/{id}/deliver` | Push/update a branch and optional pull request. |
| `/v1/tasks/{id}/reviews` | Record exact-head review verdicts. |
| `/v1/tasks/{id}/merge` | Merge only when the review gate is satisfied. |

Use `/v1/tasks/preflight` when you want repository/provider/capability
resolution without creating a task.

## Error shape

Non-2xx responses use the canonical shape:

```json
{
  "error": {
    "code": "repo_unavailable",
    "message": "repository cannot be reached",
    "retryable": false,
    "action": "fix_request"
  }
}
```

Some errors add `retry_after` and structured `details`. The complete catalog
is generated from the runtime source in [Errors](/reference/errors/).

## Idempotency and retries

Mutating task operations accept `Idempotency-Key`. The Python SDK sends keys
for mutations by default and surfaces bounded transport failures separately
from API errors. See [Idempotency](/api/idempotency/) and
[SDK errors](/sdk/python/errors/).

## Lower-level resources

`/v1/agents`, artifacts, workflow bindings, account administration and other
advanced surfaces remain public where needed, but normal product integrations
should express development work as Tasks rather than reconstruct the lower-
level agent/workspace state machine.
