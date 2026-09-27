---
title: GitHub
description: Connect private GitHub repositories and pull-request delivery through the SBX GitHub App.
---

Public repositories do not need GitHub credentials. For private repositories,
branch pushes, pull requests and merges, connect the **SBX GitHub App**.

## Default: install the SBX GitHub App once

From a configured self-hosted checkout:

```bash
./sbx github connect
```

Or open **Integrations → GitHub → Connect GitHub** in the web console.

The browser goes directly to GitHub's official installation screen for the
pre-registered SBX App:

1. choose the GitHub account or organization;
2. choose **All repositories** or **Only select repositories**;
3. click **Install**.

GitHub redirects back to SBX and the integration becomes **Connected**. This
is the normal path: no user OAuth step, PAT, PEM download, Modal Secret or
per-deployment GitHub App registration is required.

If the installation already exists, SBX can bind the deployment to that
installation without asking you to grant the same permissions again.

## What SBX stores

The self-hosted control plane keeps non-secret installation metadata and the
binding needed to request tokens. When a task needs repository access, the
trusted GitHub broker mints a short-lived installation token scoped to that
installation/repository.

The shared App private key is **not** stored in your self-hosted deployment or
agent sandbox. Installation tokens are short-lived and are injected only for
git/GitHub operations; they are not written into the repository's git config.

## Repository permissions

The public App uses the permissions required by the product workflow:

- **Contents: read/write** — clone/fetch/push branches;
- **Pull requests: read/write** — open/update/read pull requests;
- **Metadata: read** — granted by GitHub.

Use GitHub's installation settings to change which repositories the App may
access.

## Verify the connection

```bash
curl "$SBX_BASE_URL/v1/github/app" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

The response reports connection/installations without exposing tokens or
private keys. In the console, **Integrations → GitHub** presents the same
canonical state as Connected / Needs attention / Reconnect / Manage
repositories.

## Use GitHub from a task

Declare the repository and delivery target; no GitHub token belongs in the
task payload:

```python
from sbx.sdk import SbxClient

client = SbxClient()
created = client.tasks.create(
    "Add request logging and tests",
    source={"repo": "https://github.com/acme/private-api"},
    delivery={"pull_request": {"title": "Add request logging"}},
)
```

Use `client.tasks.preflight(...)` first when you want to verify repository
read/push capability without allocating a sandbox.

## Reconnect or revoke

If an installation is suspended, removed, or no longer covers the requested
repository, reconnect from the Integrations page or run `./sbx github
connect` again. Admin APIs can sync or revoke recorded installations; see the
[REST reference](/reference/api/).

## Advanced alternatives

The default public-App flow is intentionally the simplest path. Advanced
self-hosters can instead use:

- a **deployment-owned GitHub App** created through GitHub's Manifest flow;
- an **operator-supplied GitHub App**;
- a **PAT** as a compatibility/fallback token source.

Those modes require managing their own long-lived secret material and should
be used only when the public SBX App/broker model is not acceptable for the
deployment. The exact configuration knobs are in
[Configuration](/self-hosting/configuration/) and the generated API/CLI
references.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `repo_unavailable` during preflight | The installation exists, covers the repository, and the canonical deployment is current. |
| Clone works but push/PR does not | The installation must have Contents read/write and Pull requests read/write. |
| Integration says Needs attention | Reconnect or manage the GitHub installation, then sync again. |
| A moved/renamed repo fails | Re-run preflight so SBX canonicalizes the current repository/ref. |
