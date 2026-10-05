---
title: Repositories, revisions and delivery
description: Run a task on a repository, get its changes as a revision, publish a branch or pull request, and merge only after an exact-sha review.
---

A [task](/guides/tasks/) can work on a git repository: the control plane
clones it inside the task's sandbox, tracks every commit the work moves
through, and delivers the result as a durable **revision** you can push to a
branch, open as a pull request, review, and merge.

Everything on this page is optional. A task without a `source` runs in a
plain sandbox directory.

## Source: what to work on

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": {"text": "Fix the flaky date test"},
    "source": {"repo": "https://github.com/acme/api"}
  }'
```

| Field | Meaning |
| --- | --- |
| `repo` | Any git URL the sandbox can reach, or a filesystem path. `https://github.com/…` and `git@github.com:…` forms are canonicalized to the same repository. |
| `ref` | `auto`/`HEAD`/absent → the repository's default branch. A branch or tag resolves to its sha; a 40-hex sha pins that commit. |

The control plane resolves the ref to an **exact commit** before the run
starts — you never write `base_sha` yourself — and persists it on the task's
`resolved` evidence. If the repo is unreachable or the ref does not exist,
the task fails with `invalid_source` before a sandbox is allocated; run
`POST /v1/tasks/preflight` to check resolution without committing.

Public repositories and non-GitHub remotes need no credentials. Private
github.com repositories need the [GitHub integration](/integrations/github/).

## Delivery: where the result goes

`delivery` declares what happens with the work once a run leaves commits:

| Field | Default | Meaning |
| --- | --- | --- |
| `branch` | derived `sbx/…` name | The work branch the revision publishes to. |
| `auto_publish` | `false` | Push the work branch automatically after every run that finishes successfully. |
| `pull_request` | — | Open a pull request on publish: `{title, body, draft, target}`; `target` defaults to the resolved base ref. |

```json
{
  "prompt": {"text": "Fix the flaky date test"},
  "source": {"repo": "https://github.com/acme/api"},
  "delivery": {
    "branch": "sbx/flaky-date",
    "pull_request": {"title": "Fix flaky date test", "draft": true}
  }
}
```

With `auto_publish`, the publish step runs after every finished run. Without
it, publish on demand:

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks/$TASK_ID/delivery" \
  -H "Authorization: Bearer $SBX_API_KEY"
```

Publishing verifies the remote branch resolves to exactly the pushed head —
drift fails closed with `repo_unavailable`. Pull requests need the GitHub
integration; pushes work with any remote the sandbox can reach.

The task's `delivery` view reports `required`/`pending`/`delivered`/`failed`
plus the recorded `pushed_head_sha`, `pull_request` and `merge` metadata.
A task whose required delivery did not land reports `delivery_failed`; retry
with `POST /v1/tasks/{id}/retry` (`mode: "delivery"`) — the run does not
re-execute.

## Revisions

Each run's result materializes into a durable **revision** — a content-row
pinning `repo`, `base_sha`, `head_sha` and the artifact carrying the diff.
Revisions survive the sandbox and are addressable per task:

```bash
curl "$SBX_BASE_URL/v1/tasks/$TASK_ID/revisions" \
  -H "Authorization: Bearer $SBX_API_KEY"
curl "$SBX_BASE_URL/v1/tasks/$TASK_ID/revisions/latest" \
  -H "Authorization: Bearer $SBX_API_KEY"   # or rev-…, or the n counter
```

`status` is `ready` once materialized or `materialization_failed` when the
changes could not be packaged (retry the task to re-materialize). The
console's task page links each revision to its diff.

## Reviews

A **review** is a durable verdict pinned to a revision:

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks/$TASK_ID/reviews" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "verdict": "approve",
    "revision": "latest",
    "comment": "Tests pass locally."
  }'
```

- `verdict` is `approve`, `request_changes` or `comment`; `findings` carries
  structured per-file notes.
- `reviewer` (`{identity, agent_id, run_id}`) defaults to the calling API
  key. A review from the revision's own agent or run is recorded but never
  counts as `independent`.
- `reviewed_head_sha` pins the exact commit that was reviewed; a review
  turns `stale` when a newer revision materializes — review again after new
  commits.
- `comment` posts a machine-readable comment on the delivered pull request.
  It is deliberately never a formal GitHub approval.

`GET /v1/tasks/{id}/reviews` lists them (`?revision=` filters).

## Merge

```bash
curl -X POST "$SBX_BASE_URL/v1/tasks/$TASK_ID/merge" \
  -H "Authorization: Bearer $SBX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"revision": "latest"}'
```

Merge lands the delivered pull request only when **all** hold — otherwise it
fails closed:

- the revision was delivered and its pull request is recorded;
- a non-stale **independent** `approve` review pins the revision's exact
  `head_sha` — `404 revision_not_found` / `409 review_required`,
  `review_stale` or `independence_violation` when not;
- the remote pull request still points at that head.

The merge commit sha is recorded on the revision's `delivery`.

## The Python SDK

```python
from sbx.sdk import SbxClient

client = SbxClient()
created = client.tasks.create(
    "Fix the flaky date test",
    source={"repo": "https://github.com/acme/api"},
    delivery={"pull_request": {"title": "Fix flaky date test"}},
)
task = client.tasks.wait(created.task.id)

revision = client.tasks.revision(created.task.id)  # latest
review = client.tasks.review(created.task.id, verdict="approve")
merged = client.tasks.merge(created.task.id)  # gated on the review
```

## Lower-level: agent workspaces

Tasks resolve onto the same machinery the agent API exposes directly:
`GET /v1/agents/{id}/workspace` reads the durable workspace record (repo,
refs, shas, resolved git policy, `pushed_head_sha`, `pull_request`, `merge`,
`publish_error`), `POST /v1/agents/{id}/git/publish` and
`POST /v1/agents/{id}/git/merge` drive it, and
`POST /v1/agents/{id}/workspace/review` pins a review commit. Agents created
via `POST /v1/agents` also accept an explicit `workspace` (`repo`, `base_ref`,
`base_sha`) and `git` policy (`push`, `auto_create_pr`, `auto_publish`,
`merge`, `target`, `draft`, `title`, `body`) for callers that need to pin an
exact starting commit — see [Agents and runs](/guides/agents-and-runs/) and
the [API reference](/reference/api/).

Handoffs — starting an agent from an artifact, a commit or a pull-request
ref — are covered in [Artifacts and handoffs](/guides/handoffs-and-artifacts/).

## Errors

| Code | Meaning |
| --- | --- |
| `invalid_source` | The task's `source.repo`/`ref` cannot be resolved. |
| `workspace_invalid` | Malformed declaration: invalid git policy, unsafe ref or workdir. |
| `workspace_not_found` | The agent has no repository. |
| `repo_unavailable` | Clone, fetch, push or pull request failed: unreachable repository, missing GitHub access, or remote drift on push. |
| `checkout_failed` | A ref or sha does not resolve in the fresh clone. |
| `base_sha_mismatch` | The resolved ref disagrees with a pinned sha, or a handed-off commit is not a descendant of the base. |
| `head_sha_mismatch` | A review or handoff head disagrees with the recorded head, or a pull request ref drifted from its pin. |
| `review_required` | Merge was requested without a qualifying review. |
| `review_stale` | The approving review predates the current revision head. |
| `independence_violation` | Only the revision's own agent/run reviewed it — not mergeable. |
| `revision_not_found` / `revision_not_ready` | Unknown ref, or the revision has not finished materializing. |
| `merge_not_allowed` | The resolved delivery policy does not permit merging. |

When they happen while a run prepares its workspace, these are also stored
on the run as a structured error with `source: "control"` and
`retryable: false`.
