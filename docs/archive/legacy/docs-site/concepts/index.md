---
title: Core concepts
description: The public Task → Run → Revision → Delivery → Review model, plus the lower-level objects beneath it.
---

## Task

A **Task** is the user-facing unit of work. It contains a prompt and may also
declare a source repository, execution preference, delivery target, metadata,
structured-output contract or advanced resources.

Normal callers start here: `POST /v1/tasks` or `client.tasks.create(...)`.
SBX resolves automatic choices — repository ref, provider, verified account,
model and other defaults — and stores the resulting evidence on the task.

## Run

A **Run** is one execution turn on the task's agent. The first run is created
with the task; follow-ups create additional runs on the same agent and resume
the provider's native session when supported.

Run state is durable. Use task polling for product state and the run's SSE
stream when you need live events.

## Revision

A **Revision** is the durable code result of a repository run. It pins the
repository, base commit, head commit and result artifact needed to inspect or
redeliver the work after the live sandbox is gone.

Each later code-changing run can materialize a new revision.

## Delivery

**Delivery** publishes a revision: normally a work branch and optionally a
pull request. It records the pushed head, PR metadata and merge result.
Delivery can be automatic or explicitly triggered after the run.

## Review

A **Review** is a durable verdict (`approve`, `request_changes` or `comment`)
pinned to a revision's exact head. If a newer revision changes the head, the
old approval becomes stale. A review produced by the same agent/run as the
revision is not considered independent for merge.

## Integration

An **Integration** is an external capability connected to the deployment:

- a verified provider account that can execute tasks;
- a GitHub App installation that covers private repositories/PR operations;
- Modal itself, which hosts the self-hosted control plane and sandboxes.

The console presents these as connection state and next action rather than raw
credential files.

## Provider and account

A **Provider** identifies a supported coding-agent CLI/runtime. An **Account**
is one verified login for that provider. Automatic scheduling picks an
eligible account with a free slot; most callers should not pin account ids.

## Lower-level objects

These are real public/advanced objects but are not the normal starting point:

- **Agent** — the live/resumable sandbox session underneath a task;
- **Sandbox** — the isolated Modal execution environment;
- **Workspace** — lower-level repository/git state for an agent;
- **Artifact** — persisted package/diff/handoff material;
- **Workflow** — durable binding/recovery metadata for multi-agent orchestration.

Use these surfaces only when the higher-level Task API does not express the
advanced operation you need.

## Durable vs ephemeral

**Durable:** task/run terminal state, account metadata, revision/review data,
workflow metadata and stored result artifacts.

**Ephemeral:** the live sandbox process tree, temporary provider home state,
short-lived GitHub tokens and other execution-only material.

The durable contract is what lets clients recover after sandbox or
control-plane restarts without guessing what happened.
