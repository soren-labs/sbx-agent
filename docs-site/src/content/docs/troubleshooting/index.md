---
title: Troubleshooting
description: Common problems and solutions.
---

## Deployment issues

### `sbx init` fails: "Python 3.12+ required"

**Fix:** Install Python 3.12 or newer.

```bash
python --version
pyenv install 3.12.0  # if using pyenv
```

### `sbx deploy` fails: "Modal credentials not found"

**Fix:** Authenticate with Modal:

```bash
uv run modal token new
# or export MODAL_TOKEN_ID + MODAL_TOKEN_SECRET
uv run sbx doctor
```

### `sbx deploy` fails: "Secret sbx-codex-auth not found"

**Fix:** Create the shared Codex credential Secret:

```bash
modal secret create sbx-codex-auth \
  CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"
uv run sbx deploy
```

(Only needed if `codex` is in `SBX_PROVIDERS`.)

### Image build timeout

**Fix:** Modal may be slow or capacity-constrained. Retry:

```bash
uv run sbx deploy
```

If persistent, check Modal status and consider deploying at a different time.

## Runtime issues

### `429 concurrency_limit` when creating agents

**Fix:** Close idle agents or increase the cap:

```bash
curl -X DELETE "$SBX_BASE_URL/v1/agents/{idle-id}" \
  -H "Authorization: Bearer $SBX_API_KEY"

# Or increase globally
export SBX_MAX_CONCURRENT="16"
uv run sbx deploy
```

### `409 account_unavailable` — named account cannot take the agent

The account you named is missing, belongs to another provider, or is not
`active` (for example `cooling` after a rate limit — 15 minutes by default —
or `invalid` after an auth failure).

**Fix:** check its status, then use another account or `account_id: "auto"`:

```bash
# List accounts
curl -X GET "$SBX_BASE_URL/v1/accounts" \
  -H "Authorization: Bearer $SBX_API_KEY"

# Use a specific account
curl -X POST "$SBX_BASE_URL/v1/agents" \
  -d '{"agent": {"account_id": "devin-test"}}' ...
```

### Run hangs or never starts

**Possible causes:**
1. **Sandbox creation is slow** — Modal may be provisioning. Wait 30+ seconds.
2. **Turn timeout is too short** — increase `SBX_TURN_MAX_SECONDS`.
3. **Control plane crashed** — check logs:
   ```bash
   modal app logs sbx-control --tail 50
   ```
4. **Sandbox lost** — the run will eventually expire and return `UNKNOWN`. For a task, `POST /v1/tasks/{id}/retry` re-runs it on a fresh sandbox; for a bare agent, retry on a fresh one.

### `ERROR: event_parse_error` — event stream corrupted

**Cause:** Provider CLI output was malformed JSON (rare).

**Fix:** The run is unrecoverable. Check the agent status and retry on a new agent.

### Run times out (exit code 3)

**Fix:** Increase the per-turn timeout:

```bash
export SBX_TURN_MAX_SECONDS="1800"  # 30 min instead of 15
uv run sbx deploy
```

Or request more compute on the task:

```json
{
  "prompt": {"text": "..."},
  "compute": {
    "cpu": [2, 4],
    "memory_mib": [2048, 16384]
  }
}
```

## Credential issues

### `auth_invalid` on every run

**Fix:** Verify the credential:

```bash
uv run sbx auth verify --account-id devin-1
# or: curl -X POST "$SBX_BASE_URL/v1/accounts/{id}/verify" \
#   -H "Authorization: Bearer $SBX_API_KEY"
```

If `auth_invalid`, sign in again and refresh the stored credential — the
shortest path is `sbx auth relink` (re-capture → refresh → verify). The
explicit form:

```bash
# Re-login with the provider locally
codex login
devin  # or `agy`, `grok`, `opencode auth login`

# Re-import
uv run python -m control.onboarding --modal import \
  --provider devin \
  --from ~/.local/share/devin/credentials.toml \
  --account-id devin-1

# Verify again
curl -X POST "$SBX_BASE_URL/v1/accounts/devin-1/verify" ...
```

### `rate_limited` repeatedly

**Fix:** The provider is throttling your account. Options:

1. **Wait** — the scheduler automatically retries with exponential backoff (see `retry_after`)
2. **Add accounts** — scale to multiple provider accounts with `SBX_<PROVIDER>_ACCOUNTS`
3. **Reduce concurrency** — lower `SBX_MAX_CONCURRENT` or per-provider slots

### Secret material leaked in run

**Fix:** If you accidentally committed a credential to the repo and an agent saw it:

1. **Rotate the credential immediately** (new API key, new token, etc.)
2. **Remove from git history:**
   ```bash
   git filter-repo --invert-paths --path .env
   git push --force
   ```
3. **Block future leaks** — add to `.gitignore` and use `pre-commit` hooks

## Network issues

### SSE stream drops frequently

**Cause:** Network instability or server timeout.

**Fix:** Implement exponential backoff and `Last-Event-ID` reconnect (see [Streaming](/guides/streaming/)):

```python
for event in client.watch(agent_id, run_id, read_timeout_s=120):
    # Automatic reconnect with Last-Event-ID
    pass
```

### `curl` hangs on SSE stream

**Fix:** SSE is a long-lived connection. This is normal. Interrupt with `Ctrl+C` or set a read timeout:

```bash
timeout 300 curl -X GET "..." --no-buffer
```

## Control plane issues

### `GET /v1/me` fails — control plane unreachable

**Fix:** Check if the app is running:

```bash
modal app list | grep sbx-control

# View logs
modal app logs sbx-control --tail 100

# Restart
modal app stop sbx-control
uv run sbx deploy
```

### Reaper never cleans up idle agents

**Fix:** The reaper runs every 5 minutes. Check logs:

```bash
modal app logs sbx-control --tail 100 | grep reaper
```

If the reaper is stuck, restart the app:

```bash
modal app stop sbx-control
uv run sbx deploy
```

## Debugging

### Enable verbose logging

```bash
export LOGLEVEL=DEBUG
uv run sbx doctor
uv run sbx deploy
```

### Check sandbox logs

If an agent seems stuck, view the sandbox's container logs (sandboxes run as Modal containers):

```bash
modal container list
modal container logs <container-id>
```

### Inspect durable state

Check the run ledger:

```bash
modal dict list
modal dict items sbx-runs
```

(Dicts are binary; use Modal CLI or the Python API to inspect.)

### Report a bug

File an issue on GitHub with:
- sbx-browser version (`git log -1 --oneline`)
- Reproduction steps
- Command output (sanitize any credentials)
- Modal app logs (`modal app logs sbx-control --tail 100`)
