# Repo workflows — workspaces, handoffs, GitHub auth

How an agent gets a repository to work on, how work moves between agents,
and when (if ever) GitHub credentials are involved. The frozen contracts are
[contracts/api-v1.yaml](contracts/api-v1.yaml) (`workspace` / `handoff` /
`artifacts` / `workspaces` routes) and
[contracts/artifacts.md](contracts/artifacts.md); this file is the
operator-facing narrative.

## Declaring a workspace

`POST /v1/agents` accepts `workspace: {repo, base_ref, base_sha}`:

- `repo` — what the control plane clones inside the sandbox (a URL or a
  filesystem path).
- `base_ref` — the ref `base_sha` is expected to sit at.
- `base_sha` — the exact 40-hex commit run-1 must start on.

Prepare is explicit-failure: clone → resolve `base_ref` → compare against
`base_sha` → checkout. A resolved ref that disagrees fails run-1 with
`base_sha_mismatch` (run `ERROR`, agent closed) — the agent never silently
works on the wrong version. `GET /v1/agents/{id}/workspace` returns the
durable `WorkspaceRecord` (`checkout_sha`, `head_sha`,
`reviewed_head_sha`).

```python
created = client.create(
    "Fix the flaky date test",
    provider="codex",
    workspace={
        "repo": "https://github.com/owner/repo",
        "base_ref": "main",
        "base_sha": "<40-hex sha>",
    },
)
```

## GitHub-less (default)

The bridge is **off by default** and the common case needs nothing:

- **Public repos** — an `https://github.com/owner/repo` (or any reachable
  public git URL) clones anonymously; no token exists anywhere near the
  sandbox.
- **Non-GitHub / local remotes** — `repo` may be any URL the sandbox can
  reach or a filesystem path; the credential helper only ever covers
  `https://github.com`.
- **Cross-agent handoff without a shared remote** — snapshot the workspace
  into a durable artifact and hand it to the next agent. Source moves as a
  sha256-verified package, never as text pasted into a prompt:

```python
art = client.create_artifact(agent_a_id)  # manifest + patch.diff + repo.bundle
b = client.create(
    "Review and extend the change",
    provider="devin",
    workspace={"repo": REPO, "base_ref": "main", "base_sha": BASE_SHA},
    handoff={"artifact_id": art["artifact_id"]},
)
```

  Handoff validation is ordered: manifest → `repo` match → payload sha256 →
  workspace HEAD must equal the artifact's `base_sha` → apply (`repo.bundle`
  fetches and checks out the exact `head_sha`; otherwise `git apply` +
  commit) → per-file sha256 re-check. A `head_sha` handoff instead checks
  out an exact commit that must be reachable in the shared repo and a
  descendant of the declared `base_sha`.

- **Artifacts outlive sandboxes** — `GET /v1/artifacts/{id}/download`
  (`?member=patch.diff` / `repo.bundle` / `manifest.json` / `files/<path>`)
  works after the producing agent is closed, so a client can pull
  `repo.bundle` and push the exact head to a remote the sandbox cannot
  reach.
- **Review pinning** — `POST /v1/agents/{id}/workspace/review {head_sha?}`
  pins `reviewed_head_sha`; omitting it pins the recorded head, and a value
  that disagrees is an explicit `409 head_sha_mismatch` — a reviewed version
  is never silently mislabeled.

## GitHub repo-native (optional, opt-in)

For **private** github.com work — agent `git clone`/`fetch`/`push` and
opening pull requests (`control.workspace.create_pull_request`, the GitHub
REST API from inside the sandbox) — arm the bridge on the control plane:

```bash
export GH_TOKEN=...              # or GITHUB_TOKEN
export SBX_GITHUB_EPHEMERAL=1    # explicit opt-in — the token alone injects nothing
```

Mechanics and guarantees:

- The token travels **env only**: a `GIT_CONFIG_*` credential helper scoped
  to `https://github.com` answers git's credential prompt with
  `x-access-token` / `$GH_TOKEN` at runtime. The value never lands in argv,
  git config, or a cloned repo's `.git/config`, and caller-supplied exec env
  can neither inject nor override the GitHub-owned keys.
- The helper covers **HTTPS** github.com operations only — SSH remotes are
  untouched; declare `https://github.com/…` URLs when you want the token
  used. `GIT_TERMINAL_PROMPT=0` turns a missing credential into a fast
  failure instead of a hang.
- It applies to **every provider's** sandbox — it is not Devin-specific.
- Without the gate nothing is injected: public-repo workspaces behave
  identically and PR creation fails fast with `repo_unavailable`.
- A **remote** (`sbx deploy`ed) control plane cannot see the deploy host's
  env — store the token as a Modal Secret and name it; `sbx deploy` fails
  fast if the named Secret is missing:

```bash
modal secret create sbx-github GH_TOKEN='<fine-grained PAT>'
export SBX_GITHUB_EPHEMERAL=1 SBX_GITHUB_SECRET_NAME=sbx-github
uv run sbx deploy
```

- `sbx init` / `sbx doctor` print an advisory `github` check: which auth
  source exists (the `GH_TOKEN`/`GITHUB_TOKEN` var name, or `gh auth status`
  under `--verify`) and whether the gate is armed — never token material.

**Least privilege:** prefer a fine-grained PAT or GitHub App token scoped to
the repositories agents work on — `Contents: read/write`, plus
`Pull requests: read/write` only if agents open PRs — with the shortest
practical lifetime. Nothing is persisted in Modal beyond the Secret you
named; a classic `repo`-scoped PAT works but is broader than needed.

### GitHub App one-click authorization (SOR-177)

The preferred source is a **GitHub App**: the operator configures the App
once, and each repo owner authorizes it in the browser — no PAT handling at
all. The control plane mints **short-lived installation tokens** server-side
from the App's private key; the same `GIT_CONFIG_*` seam above injects them
identically to an env PAT (the env bridge stays as the compatibility
fallback and takes precedence when `GH_TOKEN`/`GITHUB_TOKEN` is set).

Configure the App on the control plane:

```bash
export SBX_GITHUB_APP_ID=123456
export SBX_GITHUB_APP_SLUG=my-sbx-app
export SBX_GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n..."  # remote: inside the named Modal Secret
export SBX_GITHUB_EPHEMERAL=1    # same opt-in gate — App tokens inject only when armed
```

Flow (all under `/v1/github/app`, Bearer auth):

1. `POST /v1/github/app/authorize` → `{authorize_url, state, expires_at}` —
   open `authorize_url` in a browser and pick the account/repos to grant.
   `state` is a single-use credential (600 s TTL).
2. `POST /v1/github/app/authorize/callback` with
   `{installation_id, state}` — records the installation's **selected-repo
   authorization metadata** (installation id, account, `repository_selection`,
   repo list) in the durable `sbx-github-app` store.
3. `GET /v1/github/app` reports posture (configured?, installations,
   `bridge_token` fallback presence) — names/ids/repos only, never key or
   token material. `POST /v1/github/app/sync` re-reads GitHub truth and
   drops installs deleted upstream (admin).
4. `DELETE /v1/github/app/installations/{id}` revokes (admin): best-effort
   uninstall on GitHub, then always forgets the record + cached tokens.
   **Reconnect** is authorize → callback again.

Token safety: the private key and minted tokens travel **env/Secret only** —
never argv, disk, logs, or API responses. Minted tokens are cached only in
process memory and refreshed 120 s before expiry; when the workspace repo is
known the mint is scoped to that repo (`repositories=[name]`), otherwise to
the installation's own selection. An installation whose selection doesn't
cover the repo authorizes nothing — the seam falls through to the env PAT
or fails closed (`repo_unavailable`).

## Git policy — first-class branch/push/PR (SOR-128)

`POST /v1/agents` accepts an optional `git` policy next to `workspace`:

```python
created = client.create(
    "Fix the flaky date test",
    provider="codex",
    workspace={"repo": REPO, "base_ref": "main", "base_sha": BASE_SHA},
    git={
        "branch": "sbx/flaky-date",  # work branch, created on base_sha
        "push": True,  # publish enables `POST .../git/publish`
        "auto_create_pr": True,  # open a PR after push (requires push)
        "target": "main",  # PR base; default: workspace base_ref
        "draft": True,
        "title": "Fix flaky date test",
    },
)
```

- `git` requires `workspace`; `auto_create_pr` requires `push`. Branch,
  target and handoff refs are validated for safe git-ref characters.
- `branch` materializes during prepare (`checkout -B` on `base_sha`) and is
  recorded on the durable `WorkspaceRecord` alongside the resolved `git`
  policy.
- `POST /v1/agents/{id}/git/publish` executes the publish half: refresh
  head → push `HEAD` to `refs/heads/<branch>` on the workspace repo's
  remote → verify `ls-remote` resolved to exactly the pushed head (drift
  fails closed `repo_unavailable`) → when `auto_create_pr`, open the PR via
  the opt-in GitHub bridge. `pushed_head_sha` and structured
  `pull_request` metadata (`number`/`url`/`ref`/`head_sha`/`target`/
  `draft`) persist on the record. Works with plain file-path remotes for
  push; PR creation needs the GitHub bridge armed.
- **Reviewer start from a PR ref** — `handoff.pull_request` (on create or
  `POST /v1/agents/{id}/handoff`) carries `{ref, head_sha}`; the ref is
  fetched (`refs/pull/<n>/head`, `pull/<n>/head`, or a branch name) and
  must resolve to exactly `head_sha` — a ref that drifted since the pin
  fails closed as `409 head_sha_mismatch`.
- **Review is a comment, never an approval** — `POST .../workspace/review`
  accepts `comment`, posted as a machine-readable PR issue comment through
  the recorded PR's `comments_url`. Every sandbox shares one GitHub
  identity, so a formal GitHub review approval would read as the PR author
  approving their own work — the API deliberately has no approve path.

## Errors

Workspace/handoff failures are structured `{error:{code,message}}`; during
run-1 they persist on the durable run ledger (`source=control`,
`retryable=false`, message carries the machine code):

| Code | Meaning / fix |
| --- | --- |
| `repo_unavailable` | Clone/fetch/push/PR failed — repo unreachable, auth missing (private github.com without the bridge armed), or the remote resolved to a different sha than the head just pushed. |
| `workspace_invalid` | Malformed declaration — e.g. a `handoff` without `workspace` on create, an unsafe workdir, or an invalid `git` policy (`auto_create_pr` without `push`, unsafe ref names). |
| `workspace_not_found` | The agent declared no workspace. |
| `checkout_failed` | `base_ref`/`base_sha` does not resolve in the fresh clone. |
| `base_sha_mismatch` | `base_ref` resolved to a different commit than the declared `base_sha`, or a `head_sha`/`pull_request` handoff is not a descendant of the base. |
| `head_sha_mismatch` | Handoff/review head disagrees with the recorded workspace head, or a fetched `pull_request` ref drifted from its pinned `head_sha`. |
| `artifact_not_found` / `artifact_invalid` / `checksum_mismatch` | Handoff artifact is missing, malformed, or failed integrity checks. |
| `artifact_secret` | Snapshot collection found credential-shaped content — refused fail-closed, nothing persisted. |
