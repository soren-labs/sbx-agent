---
title: Error Reference
description: HTTP error codes and run error codes.
---

## HTTP errors

All errors follow this shape:

```json
{
  "error": {
    "code": "error_code",
    "message": "human-readable message",
    "retry_after": 45
  }
}
```

### Client errors (4xx)

| Status | Code | Meaning | Fix |
| --- | --- | --- | --- |
| 400 | `invalid_provider` | Malformed request or unsupported provider | Check request body, provider name, model ID |
| 400 | `invalid_resource` | Unknown secret, MCP, or out-of-bounds compute | Check `SBX_RESOURCE_SECRETS`, `SBX_MCP_REGISTRY`, compute bounds |
| 400 | `invalid_output_contract` | Output contract schema invalid | Fix JSON Schema; use allowed keywords only |
| 401 | `unauthorized` | Missing or invalid API key | Check `Authorization: Bearer sbx_<key>` |
| 403 | `forbidden` | API key lacks required scope | Use an `admin` scope key for accounts/verify endpoints |
| 404 | `not_found` | Agent, run, or artifact doesn't exist | Check ID spelling |
| 409 | `turn_in_progress` | Agent is already running a turn | Wait for the current run to finish |
| 409 | `session_not_runnable` | Agent is closed/timed out | Open a new agent or use handoff |
| 409 | `account_unavailable` | Named account is missing, wrong provider, or not active | Check account ID and status, or use `account_id: "auto"` |
| 409 | `account_busy` | Named account has no free slots | Wait or use another account |
| 409 | `review_required` | Merge requires review approval on current head | Request review or disable gating |
| 409 | `artifact_secret` | Artifact collection failed due to secret material | Remove `.env`, `auth.json`, etc. |
| 429 | `concurrency_limit` | Live-agent cap reached — per API key or across the control plane (`SBX_MAX_CONCURRENT`) | Close idle agents or raise the cap |
| 429 | `provider_exhausted` | `account_id: "auto"` found no `active` account of the provider with a free slot | Wait `retry_after`, close idle agents, or add accounts |

### Server errors (5xx)

| Status | Code | Meaning | Action |
| --- | --- | --- | --- |
| 500 | (internal error) | Control-plane bug or infrastructure issue | Check `modal app logs sbx-control --tail`; retry later |
| 503 | (service unavailable) | Modal infrastructure down | Retry later |

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
