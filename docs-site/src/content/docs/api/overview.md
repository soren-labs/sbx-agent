---
title: API overview
description: The single /api surface - authentication, CSRF, Idempotency-Key, status codes, errors and pagination.
---

There is **one business API**, under `/api`. `/healthz` and `/readyz` are
operational endpoints. There are no versioned or alternate API prefixes. The
reference is generated from `docs/specs/unified/openapi.yaml` and is browsable
under [REST API (/api)](/reference/api/).

## Authentication

Two methods resolve to the same principal and permissions:

| Method | How | Extra requirements on mutations |
| --- | --- | --- |
| API key | `Authorization: Bearer sbx_key_…` | The key's scope must allow the call (see below) |
| Cookie session | `POST /api/auth/login` sets `sbx_session` (HttpOnly) and `sbx_csrf` | `X-CSRF-Token` header equal to the `sbx_csrf` cookie, and an allowed `Origin` |

Create a key with `POST /api/api-keys` (cookie session). The plaintext is
returned once and only a hash is stored. `scopes` is a non-empty subset of
`["*", "write", "read"]` (default `["*"]`; anything else is `422`):

| Scope | Allows |
| --- | --- |
| `read` | `GET` routes only; every mutation returns `403 forbidden` |
| `write` | reads and all resource mutations |
| `*` | everything, including minting/revoking API keys and changing the password |

Cookie sessions are always full scope. Login requires a verified email, is
rate limited to 10 failures per email per 15 minutes (`429 rate_limited`), and
changing a password revokes cookie sessions.

## Idempotency-Key

Every mutation requires an `Idempotency-Key` header (up to 200 characters), or
the call fails with `validation_failed`.

- Same key and same body returns the original committed response.
- Same key and a different body returns `409 idempotency_conflict`.
- Records are kept for 7 days.
- After a network error, retry with the **same** key. The SDK does this for you.

## Responses

- `201` for immediate creates.
- `202` for accepted long operations, with resource ids, `operation_id` or
  `job_id` when meaningful, and `event_watermark`. Poll `/api/operations/{id}`
  or follow events.
- `204` for logout, which has no body.
- Responses carry `X-Request-Id` and `Cache-Control: no-store`.

## Errors

```json
{"error": {"code": "version_conflict", "category": "concurrency",
           "message": "session version changed", "retryable": true,
           "details": {"current_version": 4}, "request_id": "req_ab12cd34ef56ab12"}}
```

`retry_after` (also sent as `Retry-After`) and `action` appear when relevant.
Request bodies are never echoed. Branch on `code`, not on `message`. See
[Errors](/api/errors/) for all codes.

## Concurrency and pagination

Updates carry an `expected_version`; a stale value returns `version_conflict`.
Lists are bounded and ordered deterministically. Event listings use `after`
and `limit` instead; see [Events](/api/events/).

## Reads do not do work

`GET` requests never provision compute or settle state. Files, terminals and
services return `executor_unavailable` when no live lease exists; use
`POST /api/sessions/{id}/executor/activations` to wake one.

## Not available

`POST …/services/{name}/preview-grants` exists in the schema but returns an
error because no dedicated preview origin is configured. Terminals use
polling (`…/terminals/{id}/input` and `…/output`).
