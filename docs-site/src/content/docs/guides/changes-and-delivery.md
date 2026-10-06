---
title: Changes and Delivery
description: Capture immutable ChangeSets, deliver them as pull requests, and merge only the exact subject that was reviewed.
---

## ChangeSets

A ChangeSet is an immutable snapshot of what changed in a Worktree.

- **Capture** runs as the `changeset.capture` Job. It starts automatically
  after a successful Turn in developer, integration or coordinator Sessions that
  have a repository, or on request with `POST /api/sessions/{id}/changesets`.
  A capture whose source Turn failed, was cancelled or interrupted is recorded
  with origin `salvage` and is not eligible for automatic delivery.
- The runtime writes the exact tree and reports a manifest, per-file SHA-256
  digests, the patch and file blobs. Credential files such as `.env`,
  `auth.json` and private keys are excluded; templates such as `.env.example`
  are preserved. Selected credentials and obvious secret patterns fail capture.
- A Turn completes only after its managed CLI descendants stop. Capture and
  checkpoint close managed terminals and stop services before copying state.
  An unconfirmed writer stop blocks the operation.
- Before sealing, the control plane rebuilds the manifest from its own pinned
  repository and base commit, recomputes `subject_digest` and re-verifies every
  blob. A failed capture is `failed`; there is never a fake ready subject.
- States include `ready` and `failed`. A sealed ChangeSet cannot be modified.

```python
cs = client.changesets.wait_ready(session_id, source_turn_id=turn["id"])
print(client.changesets.diff(cs["id"]))
```

`GET /api/sessions/{id}/changes` shows live, uncaptured changes without waking
compute. `POST /api/changesets/{id}/applications` applies a ChangeSet to another
Session only if its working tree equals the ChangeSet baseline; otherwise nothing
is written.

## Delivery

`POST /api/changesets/{id}/deliveries` requests delivery of exactly that
ChangeSet. The Delivery Job performs three steps and records each as evidence:

1. **Materialize**: fetch the pinned base, apply the patch, verify the tree
   equals the sealed tree, and create a deterministic commit.
2. **Push** to `sbx/SESSION/CHANGESET` using `--force-with-lease`. If the remote
   branch holds a different head the Delivery blocks with
   `remote_head_changed`; there is no plain force-push.
3. **Pull request**: find the existing PR for the head ref or open a **draft** PR,
   then verify its head equals the commit.

Use `retries` for a same-intent transport retry and `refreshes` to reconcile
with the remote. `delivery_unresolved` means the remote result could not be
proven yet.

## Merge gate

Merge is a separate operation, `POST /api/deliveries/{id}/merge-requests`. The
server evaluates the gate from stored rows plus a fresh remote observation. All
of the following must hold:

- The ChangeSet is sealed, the Delivery is verified, and the remote head equals
  the mapped commit equals `expected_head_sha`.
- The expected Delivery `version` matches and the merge method is allowed.
- Every required DelegationResult is pinned to this exact `subject_digest`, comes
  from an independent child Session and is valid; no `request_changes` result
  exists for this subject.
- Required checks pass on the exact head and the PR is open. A draft PR merges
  only if the request sets `mark_ready`.
- `require_base_unchanged` is unsupported, so a policy that needs it blocks.

Otherwise the call fails with `gate_blocked` and reasons. The provider merge is
called with `sha=expected_head_sha`, so a head that moved after the check is
refused by the provider too. A new ChangeSet is a new subject: earlier approvals
do not carry over (`stale_subject`).

The SDK sends the server's pins for you:

```python
delivery = client.deliveries.wait(client.deliveries.request(cs["id"])["id"])
client.deliveries.merge(delivery["id"], method="squash")
```

Humans cannot post verdicts. Review and test results come only from
[child Sessions](/guides/child-sessions/).
