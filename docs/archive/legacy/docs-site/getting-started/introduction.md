---
title: Introduction
description: The SBX product model, deployment boundary, and public interfaces.
---

SBX is **self-hosted orchestration for coding-agent CLIs**. You submit a
**task** — a prompt, optionally a repository and delivery target — and SBX
runs an official provider CLI in an isolated Modal Sandbox. The control plane
keeps durable task/run state and turns repository work into revisions that can
be delivered, reviewed and merged.

## Who it is for

- teams that want one task API in front of several coding-agent subscriptions;
- developers who want resumable cloud tasks without manually managing sandboxes;
- multi-agent systems that need durable revisions, exact-head reviews and deterministic recovery;
- self-hosters who want credentials, execution and state in their own Modal workspace.

## The public model

| Concept | What it means |
| --- | --- |
| **Task** | The user intent: prompt, optional source repo, execution preference and delivery policy. |
| **Run** | One turn on the task's agent. Follow-ups create more runs on the same native session. |
| **Revision** | A durable repository result pinned to exact base/head commits. |
| **Delivery** | The published branch and optional pull request for a revision. |
| **Review** | A verdict pinned to an exact revision head; newer revisions make older reviews stale. |
| **Integration** | A verified provider login or GitHub installation the control plane can use. |

The lower-level Agent, Sandbox, Workspace, Artifact and Workflow objects are
still available for advanced integrations, but normal callers should start
with `/v1/tasks` and `client.tasks`.

## Deployment boundary

A self-hosted deployment contains:

- **Control plane** — FastAPI, task/run ledger, scheduler, revisions/reviews and the web console;
- **Modal durable stores** — state that survives sandbox teardown and control-plane restarts;
- **Modal Sandboxes** — isolated execution environments running the provider's official CLI;
- **Provider accounts** — verified logins captured through the provider's own authentication flow;
- **Optional GitHub installation** — short-lived repository tokens minted from the installed SBX GitHub App.

SBX does not proxy model APIs or resell provider quota. Provider CLIs run
under your own subscriptions.

## Public interfaces

Use these in preference order:

1. **Web console** for interactive operation and inspection.
2. **Python SDK (`sbx.sdk.SbxClient`)** for programmatic task workflows.
3. **REST `/v1` API** for language-neutral integrations.
4. **CLI** for deployment, provider/GitHub connection and operator tasks.

The legacy/internal `/api/*` surface is not a public integration contract.

## Security boundary

The Modal Sandbox is the execution boundary. Provider credentials are
materialized only where the provider CLI needs them. Public API calls use a
Bearer `sbx_…` key; administrative operations require the `admin` scope.
GitHub installation tokens are short-lived and are not persisted into cloned
repositories.

## Version and status

Current package/docs line: **v0.1.1, public alpha**. Use the deployment's
`sbx status` output and the docs site's `/version.json` to verify which
release the documentation describes. `/v1` may still evolve before 1.0.

## Next

- [Quick start](/getting-started/quick-start/)
- [Core concepts](/concepts/)
- [Task lifecycle](/guides/tasks/)
- [Python SDK](/sdk/python/quickstart/)
