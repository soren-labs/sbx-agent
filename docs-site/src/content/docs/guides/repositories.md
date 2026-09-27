---
title: Repositories and git
description: Start an agent on an exact commit, publish its work branch, open a pull request, and merge only after an exact-sha review.
---

An agent can work on a git repository. You pin the exact commit it starts
from; the control plane clones it inside the sandbox, records every head the
work moves through, and — if you allow it — pushes a work branch, opens a pull
request and merges it after a review pinned to an exact sha.

Everything on this page is optional. Agents without a `workspace` simply work
in an empty sandbox directory.

## Declare a workspace

Pass `workspace` when you create the agent. All three fields are required:

| Field | Meaning |
| --- | --- |
| `repo` | What to clone inside the sandbox: any git URL the sandbox can reach, or a filesystem path. |
| `base_ref` | The ref `base_sha` is expected to sit on, for example `main`. |
| `base_sha` | The exact 40-hex commit run 1 must start from. |

```bash
curl -X POST "$SBX_BASE_URL/v1/agents" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": {"text": "Fix the flaky date test"},
    "agent": {"provider": "codex"},
    "workspace": {
      "repo": "https://github.com/acme/api",
      "base_ref": "main",
      "base_sha": "4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39"
    }
  }'
```

Before run 1 starts, the control plane clones `repo`, resolves `base_ref`,
compares it with `base_sha` and checks the commit out. It never silently works
on a different version: if the ref resolves elsewhere, run 1 fails with
`base_sha_mismatch` (run `ERROR`, agent closed), and a ref or sha that does
not exist fails with `checkout_failed`.

The Python client passes the same dictionary through:

```python
from examples.sbx_client import SbxClient

client = SbxClient()
created = client.create(
    "Fix the flaky date test",
    provider="codex",
    workspace={
        "repo": "https://github.com/acme/api",
        "base_ref": "main",
        "base_sha": "4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39",
    },
)
```

Public repositories and non-GitHub remotes need no credentials. Private
github.com repositories need the GitHub integration — see
[GitHub access](/integrations/github/).

## Add a git policy

`git` declares what the agent may do with its work. It requires `workspace`.

| Field | Default | Meaning |
| --- | --- | --- |
| `branch` | `sbx/<agent_id>` | Work branch, created on `base_sha` when the workspace is prepared. |
| `push` | `false` | Allow publishing: pushing the work branch to the repository's remote. |
| `auto_create_pr` | `false` | Open a pull request when publishing. Requires `push`. |
| `auto_publish` | `false` | Publish automatically after every run that finishes successfully. Requires `push`. |
| `merge` | `false` | Allow the merge endpoint. Requires `auto_create_pr`; merging still needs a review pin. |
| `target` | `workspace.base_ref` | Base branch of the pull request. |
| `draft` | `false` | Open the pull request as a draft. |
| `title`, `body` | — | Pull request title and description. |

A policy that breaks a dependency (for example `auto_create_pr` without
`push`), an unknown field, or an unsafe ref name is rejected with
`400 workspace_invalid` before any sandbox starts.

```json
{
  "prompt": { "text": "Fix the flaky date test and open a pull request" },
  "agent": { "provider": "codex" },
  "workspace": {
    "repo": "https://github.com/acme/api",
    "base_ref": "main",
    "base_sha": "4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39"
  },
  "git": {
    "branch": "sbx/flaky-date",
    "push": true,
    "auto_create_pr": true,
    "auto_publish": true,
    "merge": true,
    "draft": true,
    "title": "Fix flaky date test"
  }
}
```

:::note
The Python client's `create()` has no `git` argument. Send policies with any
HTTP client, or through `client.http.post("/v1/agents", json=body)`, which
reuses the client's base URL and key.
:::

## Read the workspace record

`GET /v1/agents/{id}/workspace` returns the durable record. It stays readable
after the sandbox is gone.

| Field | Meaning |
| --- | --- |
| `repo`, `base_ref`, `base_sha` | What you declared. |
| `workdir` | Checkout directory inside the sandbox work directory (`repo` by default). |
| `checkout_sha` | The commit that was actually checked out. |
| `head_sha` | The latest recorded head of the work. |
| `reviewed_head_sha` | The commit a reviewer pinned, if any. |
| `git`, `branch` | The resolved git policy and work branch. |
| `pushed_head_sha` | The head the last publish pushed and verified on the remote. |
| `pull_request` | Pull request metadata recorded by publish. |
| `merge` | Merge metadata recorded by the merge endpoint. |
| `publish_error` | The last publish failure, explicit or automatic. |
| `created_at`, `updated_at` | Timestamps. |

## Publish

`POST /v1/agents/{id}/git/publish` (no body) executes the declared policy:

1. Refresh the recorded head.
2. Push `HEAD` to `refs/heads/<branch>` on the repository's remote.
3. Verify that the remote branch resolves to exactly the pushed head — drift
   fails closed with `repo_unavailable`.
4. If `auto_create_pr` is set, open the pull request (or update it).

`pushed_head_sha` and `pull_request` are saved on the workspace record, so a
reviewer can pin exactly what was published. Pushing works with any remote the
sandbox can reach, including a plain file path; opening a pull request needs
the GitHub integration.

With `auto_publish`, the same publish runs after every run that finishes
successfully. It is best effort: a failure is saved as `publish_error` on the
workspace record, and the run stays `FINISHED`.

## Pin a review

A reviewer — a person or another agent — pins the exact commit they signed off:

```bash
curl -X POST "$SBX_BASE_URL/v1/agents/$AGENT_ID/workspace/review" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"head_sha": "9e1d7c0b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d", "comment": "Tests pass locally."}'
```

- Omit `head_sha` to pin the recorded head. A value that disagrees with it is
  `409 head_sha_mismatch`: a reviewed version is never silently mislabeled.
- `comment` is optional and is posted on the recorded pull request as a
  machine-readable comment. It needs the agent's live sandbox. It is never a
  formal GitHub approval: every sandbox shares one GitHub identity, so the API
  deliberately has no approve path.

In Python, `client.review_workspace(agent_id, head_sha=None)` pins a review
(without a comment).

## Merge

`POST /v1/agents/{id}/git/merge` (no body) merges the recorded pull request
only when all of these hold:

- the policy has `merge: true` and publish has recorded a pull request;
- a review is pinned — otherwise `409 review_required`;
- the recorded pull request head and the remote pull request ref still equal
  the pinned sha — otherwise `409 head_sha_mismatch`. Review again after new
  commits.

Merge metadata is saved as `workspace.merge`.

Publish, merge and handoff change files, so they need the agent to be `idle`
on a live sandbox: a running agent answers `409 turn_in_progress`, a closed
one or one without a sandbox `409 session_not_runnable`.

## Start a reviewer from a pull request

To have a second agent review published work, create it on the same
`workspace` with a `pull_request` handoff:

```json
{
  "prompt": { "text": "Review this change for correctness and missing tests" },
  "agent": { "provider": "devin" },
  "workspace": {
    "repo": "https://github.com/acme/api",
    "base_ref": "main",
    "base_sha": "4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a39"
  },
  "handoff": {
    "pull_request": {
      "ref": "refs/pull/42/head",
      "head_sha": "9e1d7c0b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d"
    }
  }
}
```

`ref` may be `refs/pull/<n>/head`, `pull/<n>/head` or a branch name. It must
resolve to exactly `head_sha`; a ref that moved since you pinned it fails with
`head_sha_mismatch`. Other handoff sources — artifacts and plain commits — are
covered in [Artifacts and handoffs](/guides/handoffs-and-artifacts/).

## Errors

Workspace errors use the standard error envelope. `workspace_not_found` is
`404`, `workspace_invalid` is `400`, and the others are `409`. When they
happen while run 1 prepares the workspace, they are also stored on the run as
a structured error with `source: "control"` and `retryable: false`.

| Code | Meaning |
| --- | --- |
| `workspace_invalid` | Malformed declaration: invalid `git` policy, unsafe ref or workdir, or a `handoff` without `workspace` on create. |
| `workspace_not_found` | The agent was created without a workspace. |
| `repo_unavailable` | Clone, fetch, push or pull request failed: unreachable repository, missing GitHub access, or the remote resolved to a different sha than the one just pushed. |
| `checkout_failed` | `base_ref` or `base_sha` does not resolve in the fresh clone. |
| `base_sha_mismatch` | `base_ref` resolved to a different commit than `base_sha`, or a handed-off commit is not a descendant of the base. |
| `head_sha_mismatch` | A review or handoff head disagrees with the recorded head, or a pull request ref drifted from its pin. |
| `review_required` | Merge was requested without a review pin. |
