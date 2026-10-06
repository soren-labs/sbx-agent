---
title: Child Sessions
description: Delegate review, test, research, security and integration work to ordinary child Sessions with validated ResultContracts.
---

A **Delegation** creates an ordinary child Session with its own Worktree and
native context. There is no special review subsystem: a review is a child
Session whose result is validated against a contract.

## Spawn

`POST /api/sessions/{id}/delegations` takes a `role` (`review`, `test`,
`research`, `security` or `integration`), optional `changeset_id` (the subject
to pin), optional context, and optionally an explicit `result_kind`. In one
transaction SBX creates the child Session, the Delegation, the pinned inputs and
the first Message and Turn carrying the ResultContract, then dispatches it.
The input ChangeSet is applied and committed locally as the child's baseline.

Limits: depth 3 and 10 children per parent.

```python
d = client.delegations.spawn(session_id, "review", changeset_id=cs["id"])
result = client.delegations.wait_result(d["delegation_id"])
```

## ResultContracts

| Role | Default contract |
| --- | --- |
| `review`, `security` | `ReviewAssessment`: `verdict` of `approve`, `request_changes` or `comment`, with findings and checks |
| `test` | `TestResult`: `status` of `pass`, `fail` or `unknown`, with checks |
| `research` | `ResearchResult`: summary and sources |
| `integration` | `IntegrationResult`: `integrated`, `conflict` or `failed` |
| other | `GenericResult` |

Subject-bound contracts echo the pinned `subject_digest`.

## Publishing a result

When the child's Turn ends, `delegation.publish_result` extracts the **last
fenced JSON block** from its output and validates kind, schema and subject pin.

- Exactly one immutable result is published. Missing or malformed output fails
  the Delegation (`output_contract_invalid`) and never counts as approval.
- For `TestResult`, the platform also runs the Project's declared checks in the
  child sandbox and overrides the status to `fail` if any check fails.
- Waiting for a Turn (`turns.wait`) and waiting for a validated result
  (`delegations.wait_result`) are different calls.

## Waits and cancel

`POST /api/delegations/{id}/waits` registers a wait; an already-available result
is checked in the same transaction, and satisfaction can queue a Message to the
parent. Cancelling a Delegation or closing the parent cancels the subtree.

Results feed the [merge gate](/guides/changes-and-delivery/) only when pinned to
the exact ChangeSet subject.

## Agent tools

Agents inside a Session reach these operations through the private tool gateway
(`/internal/tools/{tool}`) with Session-scoped grants, action allowlists and
the same depth and child budgets. The grant ends with the Session. Product
clients use `/api`, not this gateway.
