---
title: Changelog
description: Release notes for the unified SBX architecture.
---

## Unreleased

- **Modal sandboxes**: every Modal sandbox is a Linux VM, started from a
  prewarmed shared image.
- **Machine Slots**: each Slot is one official Codex subscription login, signed
  in with the official device login and kept on a private Modal Volume in your
  own Modal workspace. See [Cloud machines](/guides/cloud-machines/).
- **Sessions on a Slot**: a Session runs on a Slot, one at a time. Models come
  from the catalog the Slot's CLI reports, with the reasoning efforts each model
  supports.
- **Thinking on/off**: verified custom-API models can turn thinking on or off.
- **CLI and SDK**: `sbx slots` and `client.slots`. `sbx sessions create` accepts
  `--slot`, `--model` and `--effort`.
- **Console**: "My Cloud Machines" lists and manages Slots.

## Unified architecture (current)

SBX is built around a single domain model and a single API.

- **Sessions** are the durable unit of work; compute is a replaceable
  ExecutorLease, and the Worktree survives lease replacement through
  checkpoints.
- **One API** under `/api`, with a generated OpenAPI document, typed Console
  client, Python SDK and CLI sharing one event journal and watermark.
- **PostgreSQL** is the only business authority; slow effects run as fenced Jobs.
- **Connections** are one model for inference API keys, Modal and GitHub, with
  encrypted, versioned credentials. Inference is bring-your-own-key and
  independent of the coding CLI.
- **ChangeSets, Deliveries and Delegations**: immutable subjects, exact-subject
  merge gate and generic child Sessions with validated ResultContracts.
- **Harnesses**: OpenCode, Codex, Claude Code, Grok Build and Command Code run
  as official CLIs on your inference key; others are disabled.

Not available yet: preview origins, OAuth acquisition for Connections and a
WebSocket terminal (terminals poll).
