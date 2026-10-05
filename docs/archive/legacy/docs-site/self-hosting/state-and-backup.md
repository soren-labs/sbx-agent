---
title: State and backup
description: What is durable, what is ephemeral, and what an operator must preserve for recovery.
---

## Durable state

SBX stores task/run/account/workspace/workflow/artifact/revision-related state
in deployment-owned durable stores. Upgrades are expected to preserve those
stores unless you explicitly purge them.

The exact store names are deployment details; use `sbx status` and the Modal
workspace rather than hard-coding them into clients.

## Operator credentials

Preserve these separately from application data:

- Modal workspace authentication used by the operator/CI;
- the local durable bootstrap admin key (`~/.local/state/sbx/bootstrap.key`);
- provider logins through the managed account lifecycle;
- GitHub installation/broker binding metadata managed by the product.

Runtime-minted API keys are currently in-memory and should not be treated as
backup material; mint them again from the bootstrap admin key after a control-
plane restart when needed.

## Deployment evidence

Local SBX state records the deployed app URL/version and frozen provider CLI
version evidence. Keep your repository/tag plus that state when you need a
reproducible rollback.

## Before destructive uninstall

Use non-purge uninstall when you only want to stop compute. Purge flags erase
state/credentials intentionally and should be treated as destructive backup
boundaries.
