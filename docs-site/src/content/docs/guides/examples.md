---
title: Examples
description: Canonical executable Python examples for the public Task lifecycle.
---

The repository keeps the core documentation examples as normal Python files
under `examples/docs/`. They use only the public `sbx.sdk` surface and read
`SBX_BASE_URL` / `SBX_API_KEY` through `SbxClient`.

| Example | Purpose |
| --- | --- |
| `examples/docs/verify.py` | Verify authentication with `/v1/me`. |
| `examples/docs/create_task.py` | Create a task, optionally on a repository. |
| `examples/docs/watch_task.py` | Watch the current run and read durable task state. |
| `examples/docs/follow_up.py` | Send a follow-up and wait for the new run. |
| `examples/docs/deliver.py` | Deliver the latest revision. |
| `examples/docs/review.py` | Record a revision-pinned verdict. |
| `examples/docs/merge.py` | Request the review-gated merge. |
| `examples/docs/full_workflow.py` | Task → delivery → review → merge. |

Examples are kept deliberately small so they can be copied into automation
without hidden framework dependencies.

```bash
export SBX_BASE_URL=https://sbx.example.com
export SBX_API_KEY=sbx_...

uv run python examples/docs/verify.py
uv run python examples/docs/create_task.py \
  --repo https://github.com/owner/repo \
  "Fix the flaky test"
```

CI compiles the files, validates their imports/public SDK methods and checks
that every path referenced by the docs exists. The API/SDK implementation has
its own request/response tests against the same public contract.
