---
title: Sessions and Turns
description: Create Sessions, send Messages, follow committed events with watermarks, and handle cancel, retry and unknown outcomes.
---

## Create and send

`POST /api/workspaces/{workspace_id}/sessions` creates a Session, optionally
with an initial Message in the same transaction. The response returns the
Session, Message and Turn ids immediately; no request waits for a sandbox to
boot. Further input goes to `POST /api/sessions/{id}/messages` with routing
`queue` (default), `note` or `steer`.

```python
result = client.execute(
    "Add a CONTRIBUTING.md with one paragraph.",
    repository={"full_name": "owner/repo"},
    executor={"backend": "modal"},
)
print(result["turn"]["state"])
```

Resolution order for settings is Project defaults, then the caller's explicit
values. The OpenCode Harness defaults to the preferred free model.

## Turn lifecycle

`queued → preparing → running → succeeded | failed | interrupted`, with
`cancelling → cancelled | interrupted` after a cancel request. One Turn is
active per Session; later Messages queue. A queued or preparing Turn may carry a
`reason` such as `waiting_capacity`, `executor_unavailable` or
`credential_invalid`. Only the control plane decides terminal state, never a
process exit code.

## Events, SSE and watermarks

Every committed fact is an event with a per-Session sequence number `seq`.
Mutating responses include `event_watermark`, the highest committed sequence at
that moment. See [Events](/api/events/) for the envelope and endpoint.

- Read history with `GET /api/sessions/{id}/events?after=N` (JSON), or stream
  with `Accept: text/event-stream`. Resume with `after=N` or `Last-Event-ID`.
- Dedupe by `seq`. If the stream drops, reconnect from the last `seq` you
  processed; reconnecting never creates another Turn.
- Page backwards through history with `?before=N&limit=M`: the newest `M`
  events below `N`, in `seq` order, plus `next_before` for older ones (`null` at
  the start of the journal). The Console's Activity tab loads this way after a
  reload and merges the page with the live stream by `seq`.
- A cursor ahead of the journal fails with `invalid_cursor`.
- Tool output that PostgreSQL cannot store (NUL, unpaired surrogates) is stored
  with each such character replaced by U+FFFD and an `unrepresentable_replaced`
  count. A record the database still refuses is kept as a
  `runtime.evidence_rejected` event, so later evidence (and a cancel) is never
  held back.

```bash
sbx sessions events SESSION_ID --after 0 --follow
```

## Cancel

`POST /api/turns/{id}/cancellations` records the intent. A queued Turn is
cancelled immediately; an active Turn moves to `cancelling` and ends
`cancelled` or `interrupted`. If the Turn already finished, a committed result
wins and the call is a no-op.

## Retry

`POST /api/turns/{id}/retries` creates a new Turn from the original Message
(linked with `retry_of`). Only `failed`, `cancelled` or `interrupted` Turns of an
open Session can be retried; otherwise the call returns `invalid_transition`. A
Turn's `actions` list `retry` only when the call would be accepted, so archived
and closed Sessions never offer it.

## Archive and close

Archive hides a Session from the default lists and can be undone
(`POST /api/sessions/{id}/unarchives`). Close is permanent: it cancels running
and queued Turns, releases the Session's compute, cancels child Sessions it
started, and makes the Session read-only. History, ChangeSets and Deliveries
stay readable. The Console asks before closing and offers Archive instead.

## Listing and search

`GET /api/workspaces/{id}/sessions` pages by most recent activity
(`next_cursor`). `lifecycle` takes one state or a comma list
(`lifecycle=open,archived`); without it every lifecycle is listed. `q` searches
titles (case-insensitive) and exact Session ids across the whole workspace. The
Console's lists show open Sessions unless archived or closed ones are asked for.

## Unknown outcomes

If SBX cannot prove how a Turn ended (for example the runtime restarted
mid-operation), the Turn ends with reason `outcome_unknown` (state `interrupted`,
or `failed` if it was still preparing). SBX never relaunches it and never reports
success.

1. The SDK raises `OutcomeUnknown` from `turns.wait` and `execute`.
2. Inspect the Worktree and the Activity events to decide whether the work
   landed.
3. Acknowledge with `POST /api/turns/{id}/acknowledgements`. Until then, retry
   and the next queued Turn are held (`outcome_unknown`, action `acknowledge`),
   and acknowledgement itself waits while old compute is not yet confirmed
   isolated (retryable).

## Idempotent retries of requests

Every mutation needs an `Idempotency-Key`. Send the same key when you retry
after a network error; see [API overview](/api/overview/).

## Lifecycle commands

`archives`, `unarchives` and `closures` move a Session between `open`,
`archived` and `closed`. A closed Session rejects new work and closes its child
Sessions. `executor/activations` and `executor/releases` wake or release compute
explicitly; reads never do.
