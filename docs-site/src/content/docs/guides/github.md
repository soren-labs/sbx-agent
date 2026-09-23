---
title: GitHub access
description: Let agents clone, push and open pull requests on private GitHub repositories with a GitHub App or a personal access token.
---

Public repositories and non-GitHub remotes need no credentials: an agent's
[workspace](/guides/repositories/) clones them directly. For **private**
github.com repositories — clone, fetch, push, and opening pull requests — arm
the optional GitHub bridge with one of two token sources:

- a **GitHub App** (preferred): repository owners authorize it in the
  browser, and the control plane mints short-lived installation tokens; or
- a **personal access token** (PAT) held by the control plane.

Either way the bridge is:

- **Opt-in.** Nothing is injected unless the control plane runs with
  `SBX_GITHUB_EPHEMERAL=1`. A token alone does nothing.
- **Env-only inside the sandbox.** A git credential helper scoped to
  `https://github.com` answers `x-access-token` / `$GH_TOKEN` at runtime, so
  the token never lands in argv, git config, a cloned repo's `.git/config`,
  or any file.
- **HTTPS only.** SSH remotes are untouched — declare `https://github.com/…`
  URLs when you want the token used.
- **Provider-agnostic.** It applies to every provider's sandbox.

If both sources are configured, a `GH_TOKEN` / `GITHUB_TOKEN` on the control
plane takes precedence over the App.

## Option A: GitHub App (preferred)

### 1. Create the App

On GitHub: **Settings → Developer settings → GitHub Apps → New GitHub App**.

- **Setup URL** (post-installation redirect): your console's URL. The console
  picks up the `installation_id` and `state` GitHub appends and completes the
  authorization for you.
- **Webhook:** not used — you can leave it inactive.
- **Repository permissions:** `Contents: Read and write`, plus
  `Pull requests: Read and write` if agents open or merge pull requests.
  `Metadata: Read` is granted automatically.

Note the **App ID** and the App's **slug** (the name in
`https://github.com/apps/<slug>`), and generate a **private key** (PEM).

### 2. Configure the control plane

A deployed control plane reads the private key from a Modal Secret:

```bash
modal secret create sbx-github-app \
  SBX_GITHUB_APP_PRIVATE_KEY="$(cat private-key.pem)"

export SBX_GITHUB_APP_ID=123456
export SBX_GITHUB_APP_SLUG=my-sbx-app
export SBX_GITHUB_APP_SECRET_NAME=sbx-github-app
export SBX_GITHUB_EPHEMERAL=1
uv run sbx deploy
```

The same settings live in `~/.config/sbx/config.toml` as
`[github] ephemeral` and `[github_app] app_id`, `slug`, `secret_name`.
`sbx deploy` fails before writing anything if the named Secret does not
exist. A local control plane (`SBX_BACKEND=local`) reads
`SBX_GITHUB_APP_PRIVATE_KEY` straight from its environment instead.

### 3. Authorize repositories

In the console, open **Admin → GitHub** and click **Connect GitHub**. GitHub
opens in a new tab: pick the account and repositories, and GitHub redirects
back to the console, which records the installation. If the redirect cannot
reach the console, paste the `installation_id` from the redirect URL into the
form on the same page.

The same flow over the API (any key with the `agents` scope):

```bash
curl -X POST "$SBX_BASE_URL/v1/github/app/authorize" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

```json
{
  "authorize_url": "https://github.com/apps/my-sbx-app/installations/new?state=…",
  "state": "…",
  "expires_at": "2026-09-23T10:30:00Z"
}
```

Open `authorize_url` in a browser. `state` is single-use and expires after
10 minutes. When GitHub redirects, send the `installation_id` it appended
together with the `state`:

```bash
curl -X POST "$SBX_BASE_URL/v1/github/app/authorize/callback" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"installation_id": 12345678, "state": "…"}'
```

The control plane stores only the installation's metadata — id, account, and
which repositories it covers. Tokens are minted on demand per repository,
cached in memory and refreshed before they expire. A workspace repository
that no installation covers gets no App token.

### 4. Manage installations

| Action | Request | Scope |
| --- | --- | --- |
| Posture: configured?, installations, whether a bridge PAT exists | `GET /v1/github/app` | `agents` |
| Re-read installations from GitHub, dropping ones deleted there | `POST /v1/github/app/sync` | `admin` |
| Revoke one installation (best-effort uninstall on GitHub, then forget it) | `DELETE /v1/github/app/installations/{installationId}` | `admin` |

Responses carry names, ids and repository lists only — never keys or tokens.
To reconnect after revoking, run the authorize flow again.

## Option B: personal access token

Create a **fine-grained** token scoped to the repositories agents work on,
with `Contents: Read and write` and — only if agents open pull requests —
`Pull requests: Read and write`, and the shortest practical expiry. A classic
`repo` token works but grants far more than needed.

A deployed control plane cannot see your shell's environment, so store the
token in a Modal Secret and name it:

```bash
modal secret create sbx-github GH_TOKEN='github_pat_...'
export SBX_GITHUB_EPHEMERAL=1 SBX_GITHUB_SECRET_NAME=sbx-github
uv run sbx deploy
```

(`[github] ephemeral = true` and `secret_name = "sbx-github"` in
`config.toml` are equivalent.) For a local control plane, export `GH_TOKEN`
(or `GITHUB_TOKEN`) and `SBX_GITHUB_EPHEMERAL=1` in its environment.

`sbx init` and `sbx doctor` include an advisory `github` check that reports
which source exists and whether the bridge is armed — never the token.

## Use it from an agent

Declare the private repository as the agent's workspace; add a `git` policy
to let the agent publish a branch and open a pull request. The Python client
has no `git` parameter, so send that body directly:

```bash
curl -X POST "$SBX_BASE_URL/v1/agents" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": {"text": "Add request logging to the API"},
    "agent": {"provider": "codex"},
    "workspace": {
      "repo": "https://github.com/example/private-repo.git",
      "base_ref": "main",
      "base_sha": "<exact commit sha>"
    },
    "git": {"branch": "agent/logging", "push": true, "auto_create_pr": true}
  }'
```

The sandbox clones the repository with the injected token. Nothing is pushed
until you publish — `POST /v1/agents/{id}/git/publish`, or automatically
after a successful run with `"auto_publish": true` — which pushes the work
branch and, with `auto_create_pr`, opens the pull request. See
[Repositories & git](/guides/repositories/) for the full policy and the
review-gated merge.

## Errors

| Code | Meaning |
| --- | --- |
| `repo_unavailable` | Clone, fetch, push or pull-request creation failed: the repository is unreachable, credentials are missing (a private github.com repo without the bridge armed), or the remote resolved to a different commit than the head just pushed. |
| `checkout_failed` | `base_ref` / `base_sha` does not resolve in the fresh clone. |
| `workspace_invalid` | The `git` policy is invalid — for example `auto_create_pr` without `push`, or an unsafe branch name. |

## Security notes

- Prefer the GitHub App: nobody handles a long-lived token, and each minted
  token is short-lived and limited to the repositories the owner selected.
- Keep private keys and PATs out of version control; on deployed control
  planes they belong only in the Modal Secrets you name.
- All sandboxes share one GitHub identity. Merge readiness is therefore
  gated by sbx-browser's own exact-commit review pin, not by a GitHub review
  approval — see [Repositories & git](/guides/repositories/).
