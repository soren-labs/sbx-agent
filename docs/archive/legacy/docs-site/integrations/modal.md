---
title: Modal
description: What runs in your Modal workspace and what the SBX deployment owns.
---

A self-hosted SBX uses your Modal workspace for the control plane, durable
stores, provider runtime images, credential Secrets and agent Sandboxes.

## Authentication

`./sbx deploy` checks the current Modal workspace. In an interactive terminal
with no existing Modal credentials it can launch the normal `modal token new`
login flow and then continue the same deployment.

For CI/non-interactive deployment, provide both `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET` through your secret-management system.

## Resources

A deployment creates/uses:

- the control-plane Modal App;
- durable Dicts for product state;
- provider runtime images;
- bootstrap/account Secrets;
- one Sandbox per live agent/task execution.

Use `./sbx status`, `./sbx doctor` and the Modal dashboard for operational
visibility. Avoid teaching normal Task clients about Modal object names; they
are self-hosting details.

## Billing boundary

Modal compute/storage/network costs belong to the workspace owner. Provider
usage is billed/limited by the provider subscription authenticated inside the
sandbox. SBX does not resell either service.
