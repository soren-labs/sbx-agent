---
title: Changelog
description: Release notes for the unified SBX architecture.
---

## Unified architecture (current)

SBX is built around a single domain model and a single API.

- **Sessions** are the durable unit of work; compute is a replaceable
  ExecutorLease, and the Worktree survives lease replacement through
  checkpoints.
- **One API** under `/api`, with a generated OpenAPI document, typed Console
  client, Python SDK and CLI sharing one event journal and watermark.
- **PostgreSQL** is the only business authority; slow effects run as fenced Jobs.
- **Connections** are one model for Modal, GitHub, OpenCode Zen and optional
  Codex, with encrypted, versioned credentials.
- **ChangeSets, Deliveries and Delegations**: immutable subjects, exact-subject
  merge gate and generic child Sessions with validated ResultContracts.
- **Harnesses**: OpenCode supported, Codex experimental, others disabled.

Not available yet: preview origins, OAuth acquisition for Connections and a
WebSocket terminal (terminals poll).
