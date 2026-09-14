# examples/

`sbx_client.py` — minimal `httpx` client for the public `/v1` API
(`docs/contracts/api-v1.yaml`, Cursor Cloud Agents shape).

```bash
SBX_API_KEY=sbx_... SBX_BASE_URL=https://sbx.sorenforge.com \
    python examples/sbx_client.py "Write hello.txt containing hi"
```

## Field mapping vs Cursor Cloud Agents API

| Cursor Cloud Agents | sbx-browser `/v1` | Notes |
| --- | --- | --- |
| `POST /v0/agents` `{prompt:{text}, source:{repository,ref}, target:{...}, model}` | `POST /v1/agents` `{prompt:{text}, agent:{provider,account_id,model}, name, idle_timeout_s}` | No VCS binding; `provider` picks the agent CLI (`codex`/`antigravity`/`grok`/`opencode`/`devin`), `account_id` picks the provider account (`"auto"` = scheduler LRU) |
| Agent `id` | `agent.id` | Same as internal session id |
| Agent `status` `CREATING/RUNNING/FINISHED/ERROR/EXPIRED` | `agent.status` `creating/idle/running/closed/timed_out/lost` | `idle` = ready for follow-up runs; `closed` = explicitly deleted (history stays read-only) |
| `POST /v0/agents/{id}/followup` | `POST /v1/agents/{id}/runs` `{prompt:{text}}` | 409 `turn_in_progress` while a run is live, 409 `session_not_runnable` on closed agents |
| `GET /v0/agents/{id}/conversation` | `GET /v1/agents/{id}/runs` + `GET .../runs/{runId}/stream` (SSE) | SSE frames `id:`/`event:`/`data:` identical to the internal `/api/*` stream; `Last-Event-ID` resumes |
| Run status `CREATING/RUNNING/FINISHED/ERROR/CANCELLED/EXPIRED` | `run.status` same enum | Derived from the turn state; `result.text` = last `agent_message` |
| `POST /v0/agents/{id}/stop` | `POST /v1/agents/{id}/runs/{runId}/cancel` | Also `DELETE /v1/agents/{id}` to reclaim the sandbox |
| Usage in run/status payloads | `GET /v1/agents/{id}/usage` + `run.usage` | `input_tokens`/`cached_input_tokens`/`output_tokens` (+ optional `cache_write_input_tokens`/`reasoning_output_tokens`), `cost_estimate_usd`, `sandbox_seconds` |
| `key_...` Bearer API key | `sbx_<key>` Bearer | Control plane stores `sha256(key)` only; plaintext returned once at `POST /v1/api-keys` |
| — | `GET /v1/models`, `GET /v1/me` | Model discovery per provider with free account counts; key identity/scopes |
| — | `/v1/accounts*`, `/v1/api-keys*` | `admin`-scoped key required (`403 forbidden` otherwise) |

Errors are always `{error: {code, message, retry_after?}}` with canonical
subcodes (`unauthorized`, `forbidden`, `not_found`, `invalid_provider`,
`turn_in_progress`, `session_not_runnable`, `account_busy`,
`account_unavailable`, `provider_exhausted`, `concurrency_limit`).
