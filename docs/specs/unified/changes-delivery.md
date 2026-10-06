# ChangeSets, Delivery and Delegation

Implements RFC 05 and the related RFC 04 tables (migration `0002_changes_delivery_delegation.sql`).

## ChangeSet

* Capture is the `changeset.capture` Job, triggered automatically after a successful Turn in
  developer/integration/coordinator Sessions with a repository, or explicitly via
  `POST /api/sessions/{s}/changesets`. A capture whose source Turn failed, was cancelled or was
  interrupted is recorded as `salvage`.
* The runtime stages changes in a private index under the exclusive barrier and writes the exact
  tree. It reports the canonical manifest, per-file SHA-256 digests, the binary patch and file
  blobs. Credential paths (`.env`, `.env.local`, `auth.json`, private keys and credential
  directories) are excluded; safe templates such as `.env.example` remain ordinary files.
  Selected credential values and obvious token/private-key patterns fail the capture.
* Before sealing, control rebuilds the manifest from its own pinned repository and base SHA,
  recomputes `subject_digest` (golden vectors in `manifests/vectors.json`), verifies every blob and
  the patch digest, and re-runs the secret guard. A failed capture becomes `failed`; there is no
  fake ready subject.
* A sealed ChangeSet is immutable at the database level. `automatic_eligible` is true only when the
  origin is `automatic` and the Turn succeeded with complete evidence.
* Apply (`changes.apply`) applies the patch only when the destination's working tree equals the
  ChangeSet baseline tree; `git apply --check` runs first, so a mismatch writes nothing. Delegation
  inputs are applied and committed locally as the child's baseline.

## Delivery

1. **Materialize**: fetch the pinned base SHA, apply the patch, verify that the tree equals the
   sealed `tree_sha`, and create a deterministic `commit-tree` (fixed SBX author/committer, the
   ChangeSet `created_at` timestamp, a message with title + ChangeSet id + subject digest).
2. **Push** to `sbx/<session>/<changeset>` with `--force-with-lease=<ref>:<expected-old>`; there is
   no plain force-push. If the remote already holds the intended commit, the step is recorded as
   already present. Any other remote head blocks with `remote_head_changed`.
3. **PR**: discover an existing PR by head ref (the body carries `<!-- sbx-delivery:… -->`) or
   create a draft PR, then verify the PR head equals the commit.

Steps are append-only evidence with the credential version used. `delivery_target_claims` serialize
platform writes per repository/ref.

**GitHub token permissions.** Delivery and merge run with the user's `github` Connection token. The
connector's validation probes repository reachability (`GET /repos/{repo}`, and
`GET /installation/repositories` for GitHub App installation tokens, which `GET /user` rejects),
so it does not prove that these write permissions are granted. A fine-grained token scoped to the Project repositories needs:

| Permission | Used for |
| --- | --- |
| Contents: read & write | fetching the base, pushing `sbx/<session>/<changeset>`, merging the PR |
| Pull requests: read & write | discovering/creating draft PRs and marking them ready (GraphQL `markPullRequestReadyForReview`) |
| Checks: read, Commit statuses: read | the merge-gate remote observation |

A classic token needs the `repo` scope. If a permission is missing, the affected step fails with
GitHub's error; nothing falls back to another credential.

Merge is a separate `merge_requests` operation. The gate (`control/domain/delivery.py`) is evaluated
from typed rows plus a fresh remote observation, using the pinned policy combined with the current
Project policy, and requires all of:

* the ChangeSet is sealed, the Delivery is verified, and the remote head equals the mapped commit
  equals `expected_head_sha`;
* the expected Delivery version matches and the merge method is allowed;
* every required DelegationResult is pinned to this exact `subject_digest`, independent and valid,
  and no `request_changes` result exists for this subject;
* required checks pass on the exact head, the PR is open, and a draft PR is only merged when the
  request explicitly marks it ready;
* `require_base_unchanged` is unsupported and therefore blocks.

The provider merge passes `sha=expected_head_sha`.

## Delegation

* `POST /api/sessions/{s}/delegations` atomically creates the child Session (own Worktree and native
  context), the Delegation, pinned inputs, and the initial Message/Turn carrying the ResultContract,
  then dispatches it. Depth is limited to 3 and children to 10 per parent.
* When the child's Turn ends, `delegation.publish_result` extracts the last fenced JSON block and
  validates kind, schema and subject pin. For `TestResult`, the platform also runs the Project's
  declared checks in the child sandbox, records the evidence, and overrides the status to `fail` if
  any check fails.
* Exactly one immutable result is published. Missing or malformed output fails the Delegation and
  never counts as approval. Humans cannot POST verdicts.
* Waits are `wait_subscriptions`. Registering checks an already-available result in the same
  transaction; satisfaction writes deduped outbox wakes and can queue a parent Message.
* Cancel and parent close cancel the subtree by closing the children.
* The tool gateway (`/internal/tools/{tool}`) exposes the RFC 07 tools over Session-scoped hashed
  grants with an action allowlist and depth/children budgets. A grant dies with the Session or an
  auth-epoch change, and tool calls dedupe by operation ID.
