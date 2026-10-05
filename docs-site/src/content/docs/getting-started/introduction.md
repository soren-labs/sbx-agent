---
title: Introduction
description: What SBX is, how its parts fit together, and what it deliberately does not do.
---

SBX runs **official provider CLIs** inside isolated executors and keeps the
durable record of the work in PostgreSQL. Its durable identity is the
**Session**; the machine that happens to run a Turn is a replaceable
**ExecutorLease**; the provider boundary is a thin **Harness** adapter to the
official CLI.

## Architecture at a glance

| Part | Role |
| --- | --- |
| Control plane (`control/`) | One FastAPI application serving the `/api` business surface plus `/healthz` and `/readyz`. Run with `sbx serve` or `python -m control.composition serve`. |
| PostgreSQL | The only business authority: journal, projections, Jobs, encrypted credential versions. |
| Job workers | Run slow or recoverable effects (provisioning, validation, capture, delivery) under fenced claims. They run in-process with `serve`, or alone with `worker`. |
| Executors | `modal` (production) and `local` (development). Both speak the same `sbx-runtime` protocol. |
| `sbx-runtime` | Supervises operations, processes and files in the sandbox and spools evidence. It contains no reasoning loop. |
| Console | A React web app that talks only to `/api`. |
| Python SDK and `sbx` CLI | Clients of the same `/api` surface. |

## What SBX does

- Accepts a Message, queues a Turn, provisions an executor if needed, runs the
  CLI, and records committed events for every step.
- Keeps the Worktree across lease replacement by sealing private checkpoints.
- Captures immutable ChangeSets, delivers them to GitHub as a branch and draft
  pull request, and gates merge on the exact reviewed subject.
- Delegates review, test, research, security and integration work to ordinary
  child Sessions with validated ResultContracts.
- Stores external credentials as write-only Connections, encrypted at rest.

## What SBX does not do

- It does not call model APIs itself or implement a tool-selection loop.
- It does not read credentials from the host environment or files.
- It does not offer preview origins or OAuth sign-in with providers yet.
  Preview-grant requests return an error naming the missing capability, and
  credentials are entered manually.
- Terminals poll for output; there is no WebSocket attach.

## Next

Start with the [Quick start](/getting-started/quick-start/), then read
[Concepts](/concepts/).
