---
title: Concepts
description: The SBX vocabulary - Session, Message, Turn, Execution, ExecutorLease, Worktree, Snapshot, ChangeSet, Delivery, Delegation, Connection and Job.
---

IDs are opaque, prefixed identifiers (for example `sess_…`, `turn_…`). An ID
conveys type, never authorization: every owned record carries a `workspace_id`
and every lookup checks it. Access to another workspace's resource is reported
as `not_found`.

## Work

**Session** (`sess_`). The durable conversation and work. It has a lifecycle
(`open`, `archived`, `closed`) and a role (`developer`, `review`, `test`,
`research`, `security`, `integration`, `coordinator`). A Session may belong to a
Project (pinned to an immutable ProjectVersion) or be projectless with an
explicit repository.

**Message** (`msg_`). An accepted piece of authored input. Routing is `note`,
`queue` (default) or `steer`. Accepted user content is immutable.

**Turn** (`turn_`). One accepted work request, ordered within the Session. States:
`queued → preparing → running → succeeded | failed | cancelling → cancelled | interrupted`.
At most one Turn is active per Session. A `reason` explains waiting or failure
(for example `waiting_capacity`, `credential_invalid`, `outcome_unknown`).

**Execution** (`exec_`). One attempt to perform a Turn on an ExecutorLease:
operational evidence, not another unit of work.

## Compute and state

**ExecutorLease** (`lease_`). A replaceable allocation of compute (`modal` or
`local`). Many historical leases may exist; at most one is live per Session.
Leases carry a generation that fences stale writers.

**Worktree** (`wt_`). The Session's single logical mutable filesystem. It
survives lease replacement because work is restored from a checkpoint, never
reset to the repository's current main.

**Snapshot** (`snap_`). An immutable manifest, either `environment` (reusable
setup for a Project version) or `checkpoint` (private Session state). Credential
files are excluded, and process, PTY and socket state is never claimed as
restored.

## Results

**ChangeSet** (`cs_`). An immutable captured subject with a manifest, per-file
digests, a binary patch and a `subject_digest`. Capture runs after a successful
Turn or on request. A capture from a failed, cancelled or interrupted Turn is
recorded as `salvage`.

**Delivery** (`dlv_`). The intent and outcome of delivering exactly one
ChangeSet to an external target (a GitHub branch and draft pull request), with
append-only step evidence. Merge is a separate, gated operation.

**Delegation** (`del_`). A parent's assignment of work to an ordinary child
Session with pinned inputs and a ResultContract. The validated, immutable
**DelegationResult** is the only thing that counts as a review or test verdict.

## Credentials and infrastructure

**Connection** (`con_`). One external authority of kind `modal`, `github`,
`inference_api`. Its secret material lives in encrypted
**CredentialVersions**; replacing a credential appends a version.

**Job** (`job_`). A durable internal continuation or effect, claimed by a worker
with a fenced generation. Jobs are not user work; status is visible through
`GET /api/jobs/{id}` and `GET /api/operations/{id}`.

**Harness**. The thin adapter to an official CLI, described by a versioned
capability manifest. See [Providers](/reference/providers/).

## Authority

Only application commands mutate state, and each resource has one writer.
Reads never settle or launch work: for example `GET` on files or services
reports `executor_unavailable` instead of waking compute. The committed Session
journal explains history; typed relational projections decide current state.
