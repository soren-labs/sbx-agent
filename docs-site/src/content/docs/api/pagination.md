---
title: Pagination
description: Cursor behavior on list endpoints and which task lists are currently unpaginated.
---

Not every v0.1.1 list endpoint uses the same paging contract.

## Tasks

`GET /v1/tasks` currently returns the caller's task list directly and does
not expose a cursor. Do not invent cursor parameters for it.

## Agents

`GET /v1/agents` returns up to the route's fixed page size plus
`next_cursor`. Pass that opaque/string cursor back as `?cursor=` until it is
null.

## Artifacts

`GET /v1/artifacts` supports `cursor` and optional `limit`. The response
contains `next_cursor`.

## Rule for clients

Treat a cursor as opaque even when the current implementation looks numeric.
Only send pagination parameters documented by that endpoint's generated
OpenAPI page. This avoids coupling clients to storage details.
