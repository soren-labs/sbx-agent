---
title: Deterministic agent workflow
description: A source-blind sequence an AI agent can follow using only public docs, a base URL and API key.
---

This page is intentionally operational. Do not inspect SBX source code or
infer hidden routes. If a documented call fails, use the documented error and
reference pages rather than guessing an internal endpoint.

## Required

```text
SBX_BASE_URL
SBX_API_KEY
TARGET_REPOSITORY   # when the task modifies a repository
```

## 1. Verify auth

```http
GET /v1/me
Authorization: Bearer $SBX_API_KEY
```

Success: HTTP 200 with the calling key/scopes.

Failure: stop and fix credentials. Do not create work with an invalid key.

## 2. Preflight the intended task

```http
POST /v1/tasks/preflight
Authorization: Bearer $SBX_API_KEY
Content-Type: application/json

{
  "prompt": {"text": "<goal>"},
  "source": {"repo": "<TARGET_REPOSITORY>"},
  "delivery": {"pull_request": {}}
}
```

Success: `ok: true` and resolved source/execution/capability evidence.

Failure: follow the canonical error `action`; do not work around a missing
repository/provider/GitHub capability by silently changing the requested
workflow.

## 3. Create the task

Use `POST /v1/tasks` with the same task intent and a stable
`Idempotency-Key` for your job.

Persist the returned `task.id`, `agent.id` and first `run.id`.

## 4. Observe progress

Primary durable read:

```http
GET /v1/tasks/{task_id}
```

Optional live stream:

```http
GET /v1/agents/{agent_id}/runs/{run_id}/stream
```

Resume the SSE stream with `Last-Event-ID` after a disconnect. The durable
task/run record decides terminal outcome.

## 5. Follow up when needed

```http
POST /v1/tasks/{task_id}/runs
{"prompt":{"text":"<follow-up>"}}
```

Wait for the new run to finish before assuming the requested change exists.

## 6. Read the durable revision

```http
GET /v1/tasks/{task_id}/revisions/latest
```

Use the revision id/head as the identity of the code result.

## 7. Deliver

```http
POST /v1/tasks/{task_id}/deliver
{}
```

Confirm the returned revision records the pushed branch / pull request.

## 8. Review independently

Record a verdict pinned to the delivered revision:

```http
POST /v1/tasks/{task_id}/reviews
{"verdict":"approve","revision":"<revision_id>"}
```

The same agent/run that produced the revision cannot satisfy the independent
merge gate.

## 9. Merge

```http
POST /v1/tasks/{task_id}/merge
{"revision":"<revision_id>"}
```

If the review is stale, review the new revision instead of bypassing the
gate.

## 10. Recover or clean up

- uncertain mutation → read durable state before retrying;
- run failure → use `/retry` with the appropriate run/delivery lane;
- active work no longer needed → `/cancel`;
- lower-level live agent no longer needed → close it through the documented
  agent/console flow when appropriate.

Use [/reference/errors/](/reference/errors/),
[/openapi.json](/openapi.json) and [/llms-full.txt](/llms-full.txt) for exact
schemas and recovery details.
