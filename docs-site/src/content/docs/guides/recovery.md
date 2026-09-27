---
title: Recovery
description: Resume after disconnects, retry failed work, and recover durable state without guessing.
---

SBX is designed so a client can recover from its durable records instead of
assuming what a timed-out request or lost browser session did.

## Lost connection while a run is active

1. Read `GET /v1/tasks/{id}`.
2. Read `GET /v1/tasks/{id}/runs` when you need the exact run history.
3. Resume the latest run stream with `Last-Event-ID`, or fall back to polling.

The terminal run/task record is the source of truth.

## Transport error on a mutation

With the Python SDK, catch `SbxTransportError`. If it names a `check` read,
perform that durable read before retrying. Keyed mutations can safely replay
the original result.

## Task failed

Use the structured task/run error to decide the lane:

- transient provider/capacity conditions: respect `retry_after` when present;
- failed code execution: `client.tasks.retry(task_id, mode="run")`;
- delivery-only failure: `client.tasks.retry(task_id, mode="delivery")`;
- invalid repository/auth/config: fix the integration/request first, then retry.

## Sandbox was reclaimed

Durable task/run/revision/review records remain. A follow-up/retry may create
or reattach the required runtime as the product contract allows; clients
should not depend on the original sandbox id.

## Multi-agent workflow recovery

The lower-level workflow API can reattach durable workflow bindings with
`client.recover(workflow_id)`. Use that only when your orchestrator truly uses
workflow bindings; ordinary Task callers do not need a workflow id.
