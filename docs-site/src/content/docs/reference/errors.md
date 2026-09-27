---
title: Errors
description: HTTP error codes and run error codes.
---

## HTTP errors

All errors follow this shape:

```json
{
  "error": {
    "code": "error_code",
    "message": "human-readable message",
    "retryable": false,
    "action": "fix_request",
    "retry_after": 45
  }
}
```

`retry_after` is present only on retryable throttling/conflict errors.

Every response body also carries `retryable` (safe to resend unchanged) and
`action` (a stable client hint). The full catalog below is generated from the
runtime source of truth — update it with
`uv run python docs-site/scripts/sync_error_reference.py`.

<!-- BEGIN GENERATED: http-error-catalog -->

Every code the API can emit, generated from the runtime catalog.

| Status | Code | Retryable | Client action | Description |
| --- | --- | --- | --- | --- |
| 400 | `account_exists` | no | `fix_request` | an account with that id already exists |
| 400 | `artifact_invalid` | no | `fix_request` | artifact request is invalid |
| 400 | `base_sha_mismatch` | no | `fix_request` | declared base sha does not match |
| 400 | `checksum_mismatch` | no | `fix_request` | declared checksum does not match |
| 400 | `connect_failed` | yes | `retry` | connect attempt failed |
| 400 | `github_app_invalid` | no | `fix_request` | github app request is malformed |
| 400 | `head_sha_mismatch` | no | `fix_request` | declared head sha does not match |
| 400 | `invalid_account_id` | no | `fix_request` | account id is not a safe identifier |
| 400 | `invalid_blob` | no | `fix_request` | credential blob is malformed |
| 400 | `invalid_compute` | no | `fix_request` | compute selection is invalid |
| 400 | `invalid_output_contract` | no | `fix_request` | output contract is unusable |
| 400 | `invalid_provider` | no | `fix_request` | provider id is not a catalog provider |
| 400 | `invalid_request` | no | `fix_request` | request is malformed |
| 400 | `invalid_resource` | no | `fix_request` | resource ref is unknown or disallowed |
| 400 | `invalid_scope` | no | `fix_request` | api-key scope is not a known scope |
| 400 | `invalid_source` | no | `fix_request` | source declaration is invalid |
| 400 | `provider_mismatch` | no | `fix_request` | account belongs to another provider |
| 400 | `schema_mismatch` | no | `fix_request` | credential does not match provider schema |
| 400 | `unknown_provider` | no | `fix_request` | provider has no onboarding descriptor |
| 400 | `unsafe_path` | no | `fix_request` | credential path is unsafe |
| 400 | `unsupported` | no | `fix_request` | requested capability is unsupported |
| 400 | `workspace_invalid` | no | `fix_request` | workspace declaration is invalid |
| 401 | `grant_invalid` | no | `authenticate` | grant is invalid, expired, or used |
| 401 | `pair_invalid` | no | `authenticate` | pair ticket is invalid or expired |
| 401 | `unauthorized` | no | `authenticate` | missing or invalid API key |
| 403 | `forbidden` | no | `authenticate` | key lacks the required scope |
| 403 | `github_app_state` | no | `authenticate` | authorize/manifest state expired |
| 404 | `account_not_found` | no | `lookup` | account does not exist |
| 404 | `artifact_not_found` | no | `lookup` | artifact does not exist |
| 404 | `delivery_not_found` | no | `lookup` | delivery record does not exist |
| 404 | `not_found` | no | `lookup` | referenced resource does not exist |
| 404 | `revision_not_found` | no | `lookup` | revision does not exist |
| 404 | `session_not_found` | no | `lookup` | connect session does not exist |
| 404 | `workspace_not_found` | no | `lookup` | workspace record does not exist |
| 409 | `account_busy` | yes | `wait` | named account has no free slot |
| 409 | `account_unavailable` | yes | `wait` | named account is not active |
| 409 | `artifact_secret` | no | `fix_request` | artifact contains secrets |
| 409 | `delivery_failed` | yes | `retry` | delivery attempt failed |
| 409 | `github_app_configured` | no | `configure` | a github app is already configured |
| 409 | `idempotency_conflict` | no | `fix_request` | key replayed with a different body |
| 409 | `idempotency_in_progress` | yes | `wait` | keyed request still in flight |
| 409 | `independence_violation` | no | `fix_request` | reviewer not independent of author |
| 409 | `merge_not_allowed` | no | `fix_request` | delivered pull request is not mergeable (e.g. draft or blocked) |
| 409 | `review_required` | no | `fix_request` | an approving review is required first |
| 409 | `review_stale` | no | `fix_request` | review targets an outdated revision |
| 409 | `revision_not_ready` | yes | `wait` | revision is not ready yet |
| 409 | `session_active` | yes | `wait` | connect session is already running |
| 409 | `session_not_runnable` | yes | `wait` | agent is not in a runnable state |
| 409 | `task_active` | yes | `wait` | task has a run in flight |
| 409 | `task_not_retryable` | no | `fix_request` | task has nothing to retry |
| 409 | `turn_in_progress` | yes | `wait` | a run is already in progress |
| 409 | `workspace_unavailable` | yes | `retry` | workspace service is unavailable |
| 429 | `concurrency_limit` | yes | `retry` | global concurrency cap reached |
| 429 | `provider_exhausted` | yes | `retry` | no free account for the provider pick |
| 500 | `internal` | yes | `retry` | internal error |
| 502 | `checkout_failed` | yes | `retry` | repo checkout failed |
| 502 | `github_app_upstream` | yes | `retry` | a github api call failed |
| 502 | `repo_unavailable` | yes | `retry` | repository could not be reached |
| 503 | `github_app_unconfigured` | no | `configure` | no github app identity configured |
| 503 | `unavailable` | yes | `retry` | required service is unavailable |

`Client action` is the stable machine-readable hint sent in every error body: `authenticate`, `lookup`, `fix_request`, `wait`, `retry`, `configure`.

<!-- END GENERATED: http-error-catalog -->

Common fixes by status:

- **401/403** — supply `Authorization: Bearer sbx_<key>`; admin-only endpoints
  also need the `admin` key scope.
- **404** — verify the resource id spelling.
- **409 `wait`** — the named object is busy; retry after it settles.
- **429** — honor `retry_after`, close idle agents, or add accounts.

## Run error codes

Terminal runs include a structured `error`:

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

### Error codes

| Code | Source | Retryable | Meaning | Fix |
| --- | --- | --- | --- | --- |
| `auth_invalid` | provider | ❌ | Credential is bad | Fix or replace account |
| `rate_limited` | provider | ✅ | Provider throttled the request | Retry with backoff (see `retry_after`) |
| `quota_exhausted` | provider | ✅ | Account quota depleted | Retry on another account or wait |
| `model_unavailable` | provider | ❌ | Model doesn't exist | Choose a different model |
| `model_capacity` | provider | ✅ | Model overloaded | Retry later |
| `provider_unavailable` | provider | ✅ | Provider API down | Retry later |
| `timeout` | runtime | ✅ | Run timed out (turn or sandbox) | Retry; may need higher compute |
| `cancelled` | control | ❌ | Run was cancelled by caller | N/A (intentional) |
| `contract_violation` | control | ❌ | Output failed JSON Schema validation (strict mode) | Fix the agent output |
| `runtime_error` | runtime | ✅ | Sandbox/runner error (unexpected) | Retry; check Modal status |
| `event_parse_error` | telemetry | ❌ | Event stream corrupt | Run is unrecoverable |

## Common scenarios

### `409 turn_in_progress`

```
POST /v1/agents/{id}/runs while a run is in progress
```

**Fix:** Wait for the current run to complete (`GET /v1/agents/{id}/runs/{runId}` until status is terminal).

### `429 concurrency_limit`

```json
{
  "error": {
    "code": "concurrency_limit",
    "message": "global live-agent cap (SBX_MAX_CONCURRENT) reached — idle agents hold slots until closed",
    "retry_after": 60
  }
}
```

**Fixes:**
1. Close idle agents: `DELETE /v1/agents/{id}`
2. Increase cap: `export SBX_MAX_CONCURRENT="16"; uv run sbx deploy`
3. Wait and retry after `retry_after` seconds

### `429 provider_exhausted`

```json
{
  "error": {
    "code": "provider_exhausted",
    "message": "no account available for provider 'devin'",
    "retry_after": 60
  }
}
```

**Fixes:**
1. Wait for running agents to finish (frees up slots)
2. Add more accounts: `control.onboarding --modal add --provider devin --from <path>`
3. Increase per-account slots when importing: `--slots 8` instead of default `--slots 1`

### `409 artifact_secret`

```
POST /v1/agents/{id}/artifacts with sensitive files
```

**Fix:** Remove or .gitignore credential files (`.env`, `auth.json`, `.modal.toml`, etc.):

```bash
rm .env auth.json credentials.toml
# Remove from git history if already committed
git filter-repo --invert-paths --path .env
curl -X POST "$SBX_BASE_URL/v1/agents/{id}/artifacts" ...
```

### `400 invalid_provider`

```
POST /v1/agents with unknown provider or model
```

**Fix:** Check `GET /v1/models` for supported models:

```bash
curl -X GET "$SBX_BASE_URL/v1/models" \
  -H "Authorization: Bearer $SBX_API_KEY" | jq '.models[] | select(.provider == "codex")'
```
