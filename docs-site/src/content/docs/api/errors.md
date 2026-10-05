---
title: Errors
description: Every canonical error code, its category, HTTP status and default retry behaviour.
---

All errors use the shape `{"error": {"code", "category", "message", "retryable",
"details", "request_id"}}`. This table is the canonical vocabulary.

<!-- BEGIN GENERATED: error-codes -->
| Code | Category | Status | Retryable |
| --- | --- | --- | --- |
| `not_found` | access | 404 | no |
| `forbidden` | access | 403 | no |
| `unauthenticated` | authentication | 401 | no |
| `csrf_failed` | authentication | 403 | no |
| `rate_limited` | throttle | 429 | yes |
| `validation_failed` | request | 422 | no |
| `version_conflict` | concurrency | 409 | yes |
| `idempotency_conflict` | request | 409 | no |
| `invalid_transition` | state | 409 | no |
| `unsupported_capability` | capability | 422 | no |
| `credential_invalid` | credential | 409 | no |
| `connection_revoked` | credential | 409 | no |
| `connection_in_use` | credential | 409 | no |
| `connection_required` | credential | 409 | no |
| `waiting_capacity` | capacity | 409 | yes |
| `quota_exhausted` | capacity | 429 | no |
| `runtime_incompatible` | runtime | 409 | no |
| `executor_unavailable` | runtime | 409 | yes |
| `context_unavailable` | context | 409 | no |
| `context_mismatch` | context | 409 | no |
| `outcome_unknown` | outcome | 409 | no |
| `output_contract_invalid` | outcome | 422 | no |
| `capture_failed` | changes | 409 | yes |
| `stale_subject` | changes | 409 | no |
| `remote_head_changed` | delivery | 409 | no |
| `delivery_unresolved` | delivery | 409 | yes |
| `gate_blocked` | delivery | 409 | no |
| `history_reset_required` | events | 409 | no |
| `invalid_cursor` | events | 400 | no |
| `stale_fence` | fencing | 409 | no |
| `operation_conflict` | runtime | 409 | no |
| `internal_error` | platform | 500 | yes |
<!-- END GENERATED: error-codes -->
