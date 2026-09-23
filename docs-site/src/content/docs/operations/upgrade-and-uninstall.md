---
title: Upgrade & Uninstall
description: Update sbx-browser and cleanly remove deployments.
---

## Upgrading

To upgrade to a new sbx-browser version:

<Steps>

1. **Fetch the latest code:**
   ```bash
   git pull
   uv sync
   ```

2. **Re-deploy (preserves durable data):**
   ```bash
   uv run sbx upgrade
   ```

3. **Verify:**
   ```bash
   uv run sbx doctor
   ```

</Steps>

`sbx upgrade` rebuilds images, updates the control plane, and restarts the Modal App — but does not touch durable Dicts. All runs, accounts, workflows, and artifacts persist.

### Rollback

To rollback provider CLI versions:

```bash
uv run sbx deploy --versions-lock <previous-lock.json>
```

Or use the environment variable:

```bash
export SBX_VERSIONS_LOCK="/path/to/previous-lock.json"
uv run sbx deploy
```

The lock file (created at deploy time as `$SBX_STATE_DIR/cli-versions.json`) captures all resolved versions. Replaying it skips upstream version checks.

## Uninstalling

### Graceful uninstall (preserve data)

Closes all agents and sandboxes, but keeps durable data:

```bash
uv run sbx uninstall
```

This:
- Closes the Modal App (stops accepting new requests)
- Closes all running sandboxes
- Preserves Dicts: `sbx-sessions`, `sbx-runs`, `sbx-accounts`, `sbx-workflows`, `sbx-artifacts`

You can re-deploy later and resume from the same durable state.

### Full uninstall (erase everything)

```bash
uv run sbx uninstall --purge-data --purge-credentials
```

This also deletes:
- All Dicts (runs, accounts, workflows, artifacts are gone)
- All account credential Secrets (imported credentials are erased)

The shared `sbx-codex-auth` and `sbx-basic-auth` Secrets are also deleted.

### Verification

After uninstall, verify cleanup:

```bash
modal app list
modal secret ls
modal dict ls
```

Should show no `sbx-*` resources. If any remain, manually delete:

```bash
modal app destroy sbx-control
modal secret rm sbx-codex-auth
modal secret rm sbx-acct-devin-1
```

## Key rotation

### API key rotation

Create a new key and revoke the old one:

```bash
# Create new key
curl -X POST "$SBX_BASE_URL/v1/api-keys" \
  -H "Authorization: Bearer $SBX_OLD_KEY"

# Revoke old key
curl -X DELETE "$SBX_BASE_URL/v1/api-keys/{old-key-id}" \
  -H "Authorization: Bearer $SBX_NEW_KEY"
```

### Account credential rotation

If an account credential leaks:

1. Create a new account with the rotated credential:
   ```bash
   uv run python -m control.onboarding --modal import \
     --provider devin \
     --from ~/.local/share/devin/credentials.toml \
     --account-id devin-new
   ```

2. Disable the old account:
   ```bash
   curl -X DELETE "$SBX_BASE_URL/v1/accounts/devin-old" \
     -H "Authorization: Bearer $SBX_API_KEY"
   ```

### GitHub token rotation

If a PAT or GitHub App secret leaks:

1. Revoke in GitHub (Settings → Developer settings)
2. Create a new token or regenerate the App secret
3. Update Modal Secret:
   ```bash
   modal secret delete sbx-github
   modal secret create sbx-github GH_TOKEN="..."
   uv run sbx deploy
   ```

## Cleanup & maintenance

### Close idle agents

Free up capacity by closing agents that are no longer needed:

```bash
curl -X DELETE "$SBX_BASE_URL/v1/agents/{id}" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

### Cleanup workflows

Scoped cleanup of a workflow group:

```bash
curl -X DELETE "$SBX_BASE_URL/v1/workflows/{id}" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

This closes all agents in the workflow and marks active runs as cancelled.

### Prune old artifacts

The control plane does not auto-prune artifacts. Monitor storage and manually delete:

```bash
curl -X GET "$SBX_BASE_URL/v1/artifacts?limit=100" \
  -H "Authorization: Bearer $SBX_API_KEY" | jq '.artifacts[] | select(.created_at < "2026-01-01") | .id' | \
  xargs -I {} curl -X DELETE "$SBX_BASE_URL/v1/artifacts/{}" \
    -H "Authorization: Bearer $SBX_API_KEY"
```
