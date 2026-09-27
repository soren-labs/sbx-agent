---
title: Troubleshooting
description: Diagnose deployment, provider, repository, task, streaming and delivery failures from the public product state.
---

Start with the layer that failed. Do not treat every provider/repository error
as a broken deployment.

## Platform/deployment

### Modal is not authenticated

On an interactive fresh clone, `./sbx deploy` can launch the normal Modal
login flow. You can also authenticate explicitly:

```bash
modal token new
./sbx deploy
./sbx doctor
```

CI/non-interactive deployments need valid `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET`.

### Platform is healthy but no provider is available

A zero-provider deployment is valid. Connect one after deployment:

```bash
./sbx auth login --provider devin
./sbx auth status
```

Provider runtime/account problems should degrade that provider, not require
manually creating provider Secrets just to make the core platform healthy.

### Custom domain shows an older Console than the backend

If the native Modal control-plane URL has the new UI but a Cloudflare edge
hostname does not, redeploy `deploy/sbx-edge`: the optional worker bundles
static `web/` files at deploy time. See [Custom domains](/self-hosting/custom-domain/).

## Authentication and provider accounts

### API key is rejected

Verify with:

```bash
curl -i "$SBX_BASE_URL/v1/me" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

If the control plane was restarted, a runtime-minted key may have been
cleared. Use the durable bootstrap admin key to mint a new scoped key.

### Provider says Needs attention / `auth_invalid`

```bash
./sbx auth verify --provider devin
./sbx auth relink --provider devin --relogin
```

Only verified accounts are schedulable. Avoid repeated Task retries until the
integration itself is healthy.

### `account_unavailable` / `provider_unavailable`

The requested provider/model/account currently has no eligible verified slot.
Prefer automatic account selection, wait for `retry_after` when present, or
connect/repair another account. Reduce concurrency when the provider itself is
throttling the subscription.

## Repository / GitHub

### `repo_unavailable` during preflight/create

Check:

1. the GitHub integration is Connected;
2. the installation covers the target repository;
3. the deployment is running the latest expected version;
4. preflight on the exact repository/ref succeeds.

```bash
./sbx github connect
```

Do **not** work around a failed `delivery.pull_request` preflight by silently
creating a different kind of task. Fix the repository capability first.

### Clone works but push/PR fails

The installation must cover the repo and have Contents read/write + Pull
requests read/write. Reconnect/manage the GitHub installation, then rerun
preflight.

### Review is stale / merge is refused

A later code revision changed the delivered head after the recorded review.
Review the newest revision, then merge again. Never bypass the exact-head gate
by copying an old approval to a new SHA.

## Task/run failures

### Task is queued for a long time

Read the task detail and structured run error, then check provider slots and
account status. A busy follow-up normally queues behind the active run; this is
not the same as a failed Task.

### Run timed out

Increase the appropriate **turn** timeout only when the work genuinely needs
more wall time; do not confuse it with post-session idle retention or the
sandbox hard timeout. Exact config fields/defaults are generated in
[`/config-reference.json`](/config-reference.json).

### Retry the right lane

- code/execution failure → `client.tasks.retry(task_id, mode="run")`;
- publish/PR-only failure after good code → `mode="delivery"`;
- invalid auth/repository/config → repair the integration/request before retrying.

See [Recovery](/guides/recovery/).

## Streaming/network

SSE disconnects are recoverable. Resume with `Last-Event-ID` or use the SDK's
run watch/resume behavior. The durable Task/Run record remains the terminal
source of truth even when the last event was lost.

For an uncertain mutation timeout, catch `SbxTransportError`, read the
suggested durable `check` resource, then decide whether a retry is necessary.
Idempotency keys protect normal SDK mutation retries.

## Artifacts / secret detection

If artifact collection returns `artifact_secret`, remove the sensitive content
from the workspace and rotate it if it was a real credential. The artifact
collector intentionally fails closed rather than persisting suspected secret
material.

## Diagnostics for operators

```bash
./sbx status
./sbx doctor
./sbx auth status
```

Then inspect Modal application logs/dashboard when the control plane itself is
unhealthy. Prefer these product/status surfaces over reading Dict/Secret
contents directly.

## Still blocked?

Collect the affected SBX version, task/run id, canonical structured error code
and the smallest reproduction. Do not include API keys, provider credentials,
GitHub tokens or Secret contents.
