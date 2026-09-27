---
title: Task lifecycle
description: Create, run, follow up, revise, deliver, review and merge through /v1/tasks.
---

The high-level lifecycle is:

```text
preflight (optional)
→ create task
→ run(s)
→ revision
→ delivery
→ review
→ merge
```

## Preflight

`POST /v1/tasks/preflight` resolves repository/provider/account/model and
checks declared capabilities without allocating a sandbox. Use it before a
user commits an expensive or permission-sensitive task.

## Create

`POST /v1/tasks` stores the requested intent and the resolved execution
choices, allocates the agent, and queues run 1.

Normal callers should provide only what matters to the job:

```json
{
  "prompt": {"text": "Fix the flaky date test"},
  "source": {"repo": "https://github.com/acme/api"},
  "delivery": {"pull_request": {}}
}
```

Provider/account/model fields default to automatic selection.

## Follow-up runs

`POST /v1/tasks/{id}/runs` sends another instruction to the same task agent.
The default busy behavior queues the follow-up; callers can request rejection
instead when they need strict one-at-a-time semantics.

## Terminal state and retry

Read `GET /v1/tasks/{id}` for product state. Retry is explicit:

- `mode=run` — execute again;
- `mode=delivery` — republish the existing result without rerunning code.

Cancel is idempotent and never erases already-published repository state.

## Revision and delivery

A repository run materializes a durable revision. Delivering the revision can
push a work branch and create/update a pull request. The revision remains
addressable after the live sandbox is gone.

## Review and merge

A review verdict pins an exact revision head. An independent approval is
required for the review-gated merge. Any later revision makes the older
approval stale.

See [Repositories and delivery](/guides/repositories/) for the full code
lifecycle and [REST reference](/reference/api/) for exact schemas.
