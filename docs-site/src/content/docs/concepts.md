---
title: Unified concepts
---
A Session is durable identity. An ExecutorLease is replaceable compute. A logical Worktree survives compute loss through explicit checkpoints. A Harness adapts an official CLI; SBX does not implement a model loop.

Current authoritative state is typed PostgreSQL projections plus committed append-only Session events. Application commands commit intent and durable Jobs. Workers execute slow effects with expiring claims, generation fences and stable effect identities. Reads never wake compute or settle effects.

ChangeSets are immutable canonical subjects; Delivery owns Git effects. Review, test, research and integration are generic Delegations with separate child Sessions and strict ResultContracts. A single Connection model stores write-only encrypted CredentialVersions.
