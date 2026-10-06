---
title: Events
description: The committed Session event journal - envelope, event types, SSE, cursors and watermarks.
---

Each Session has an append-only journal. Sequence numbers (`seq`) are allocated
inside the same transaction as the state change, so the journal and the
projections never disagree. Runtime and browser caches are never authoritative.

## Endpoint

`GET /api/sessions/{session_id}/events`

| Parameter | Meaning |
| --- | --- |
| `after` | Return events with `seq` greater than this (default 0). |
| `limit` | Page size, 1 to 1000 (default 500). |
| `types` | Comma-separated event types to include. |
| `turn_id` | Only events for one Turn. |
| `max_seconds` | SSE only: how long the stream stays open (max 600). |

Without `Accept: text/event-stream` the response is JSON:

```json
{"items": [], "next_after": 12, "event_watermark": 12}
```

With `Accept: text/event-stream` each event is an SSE frame with `id` set to
`seq`, `event` set to the type and `data` set to the JSON envelope. Comment lines
(`: watermark N`, `: heartbeat`) are keep-alives to ignore. `Last-Event-ID`
resumes like `after`; if both are given they must agree.

## Watermarks

`event_watermark` is the highest committed `seq`. Initial reads return the
snapshot and its watermark from one database snapshot, so replay from `after=W`
is gap-free. Clients dedupe by `seq`.

## Envelope

`id`, `workspace_id`, `session_id`, `seq`, `type`, `schema_version`,
`recorded_at`, `observed_at`, `actor`, `source`, `causation_id`,
`correlation_id`, `turn_id`, `execution_id`, `executor_lease_id`,
`lease_generation`, `delegation_id`, `changeset_id`, `delivery_id`,
`runtime_epoch`, `local_seq` and `payload`.

## Event types

| Family | Types |
| --- | --- |
| `session.` | `created`, `settings_changed`, `archived`, `unarchived`, `closed` |
| `message.` | `accepted`, `routed`, `part_added`, `part_updated`, `completed` |
| `turn.` | `queued`, `preparing`, `started`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, `interrupted` |
| `execution.` | `preparing`, `started`, `native_bound`, `observed_terminal`, `stopped` |
| `tool.` | `started`, `updated`, `completed` |
| `usage.`, `diagnostic.` | `usage.observed`, `diagnostic.reported` |
| `executor.` | `bound`, `quiescing`, `released`, `unavailable` |
| `worktree.` | `restored`, `changed`, `apply_requested`, `applied` |
| `snapshot.` | `requested`, `ready`, `failed` |
| `changeset.` | `capture_requested`, `ready`, `capture_failed` |
| `delivery.` | `requested`, `progressed`, `blocked`, `succeeded`, `failed`, `cancelled`, `merge_requested`, `merged`, `merge_failed` |
| `delegation.` | `created`, `waiting`, `result_published`, `failed`, `cancel_requested`, `cancelled` |
| `service.` | `requested`, `ready`, `degraded`, `failed`, `stopped` |

`message.part_updated` replaces a part's content by revision; apply it by
replacement, never by appending, to avoid duplicating cumulative text. A turn is
terminal at `turn.succeeded`, `turn.failed`, `turn.cancelled` or
`turn.interrupted`; `execution.observed_terminal` with verdict `unknown`
corresponds to an `outcome_unknown` Turn.

## Errors

`invalid_cursor` (400) for a negative cursor, a cursor ahead of the journal, or
mismatched `after` and `Last-Event-ID`. The Console treats `invalid_cursor` and
`history_reset_required` as the signal to fetch a fresh snapshot instead of
replaying.
