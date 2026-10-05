---
title: Revisions, delivery, review and merge
description: Complete the code lifecycle through client.tasks.
---

After a repository task produces code:

```python
revision = client.tasks.revision(task_id)  # latest
```

Publish/update the branch and pull request:

```python
revision = client.tasks.deliver(
    task_id,
    pull_request={"title": "Fix flaky date test"},
)
```

Record a review:

```python
review = client.tasks.review(
    task_id,
    verdict="approve",
    revision=revision.id,
    comment="Verified the regression test.",
)
```

Then request the review-gated merge:

```python
merged = client.tasks.merge(task_id, revision=revision.id)
```

A stale or non-independent approval does not satisfy the gate. Read
`client.tasks.reviews(task_id)` and the revision delivery state when a merge
is refused.
