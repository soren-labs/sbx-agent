---
title: Changelog
description: Release notes for the unified SBX architecture.
---

## Unreleased

- **API keys**: creating a key is safe to retry. A lost response returns the
  same key on retry instead of a second live key. After 10 minutes a retry
  returns `secret_unavailable` instead of a new key. The Console asks for an
  access level (Read only, Read & write, Full access) and shows each key's
  scope. `sbx auth login --scope` defaults to `write`. The SDK adds `scopes=`
  and `revoke_api_key`. `POST /api/api-keys` now needs an `Idempotency-Key`,
  like every other mutation.
- **Project versions keep their whole configuration**: publishing a new version
  from the Console changes only the fields you edit. Services, environment
  variables, reasoning effort, the agent CLI, the delivery policy and fields the
  Console does not know are kept; the API keeps unknown spec fields too. The
  dialog lists every changed field and asks you to confirm delivery-policy
  changes. Previously the Console rebuilt the spec from its visible fields and
  reset the rest to defaults, including the delivery policy.
- **Check and service commands are split like a shell, without a shell**:
  `smoke=python3 -c "print(123)"` now runs `print(123)` instead of the string
  `"print(123)"`. Quotes, backslashes, empty arguments (`''`) and `=` inside
  arguments work; the form shows the exact arguments. Empty arguments after the
  program are accepted by the API.
- **Services are declared in Projects**: the Project form has a Services
  section. The Services tab says which Project version declares a Session's
  services, explains an empty list, and states that preview links are not
  available.
- **Delivery says when it cannot work**: ChangeSets report a `delivery`
  capability. The Console no longer shows a pull request form that would fail
  (for example for a Session without a repository) and offers
  **Download patch** instead; `GET /api/changesets/{id}/patch`,
  `client.changesets.patch()` and `sbx changesets patch` export it. A Session
  that pinned no GitHub Connection delivers with the workspace's GitHub
  Connection once one is added.

- **Event ingestion**: tool output PostgreSQL cannot store (NUL, unpaired
  surrogates) no longer stalls a Session; cancel and release always settle. Such
  characters are stored as U+FFFD and counted; a record the database refuses is
  kept as `runtime.evidence_rejected`.
- **Sessions**: lists show open Sessions by default, page past the first 50 and
  search titles and ids on the server (`lifecycle=open,archived`, `q`). Closing a
  Session asks first and offers Archive. Closed and archived Sessions no longer
  offer Retry. Activity loads earlier events after a reload (`before=`).
- **Conversation**: each Turn shows its result once, even when follow-ups were
  queued while it ran; a stopping Turn says "Stopping"; startup shows the real
  stage (requesting compute, starting the sandbox, connecting, starting the CLI).
- **Children**: choosing "No ChangeSet" sticks.
- **Antigravity usage**: the CLI reports a running total per conversation; a
  resumed Turn now records only its own tokens, so session totals no longer
  double count.
- **Workspace files**: truncated or redacted file views are read-only, and saves
  always need a precondition. **Open or create file** now creates missing files.
- **Terminal**: terminal create, input and close honour `Idempotency-Key`, so a
  lost response never runs a command twice. Terminals survive panel switches, can
  be listed and closed, and render colours instead of raw escape codes.
- **Changes**: Live changes refreshes on activation, after Turns and on demand;
  diffs number only source lines and show A/M/D from the patch. Review changes
  works from every workspace tab.
- **Modal sandboxes**: every Modal sandbox is a Linux VM, started from a
  prewarmed shared image.
- **Machine Slots**: each Slot is one official subscription login, kept on a
  private Modal Volume in your own Modal workspace. Codex and Grok Build sign in
  with the official device login. See [Cloud machines](/guides/cloud-machines/).
- **Grok Build machines**: a `grok` Slot signs in with `grok login --device-auth`
  and runs Sessions on the Grok Build harness with the models its CLI reports.
- **Antigravity machines**: an `antigravity` Slot signs in with Google's official
  `agy` CLI. The user opens the Google sign-in link, signs in, and pastes the code
  the browser shows. Antigravity runs only on a machine; it has no API-key mode.
- **Paste-code login**: `sbx slots code` and `client.slots.submit_code` send the
  code shown after an Antigravity sign-in. SBX passes it to the waiting CLI and
  does not store it.
- **Sessions on a Slot**: a Session runs on a Slot, one at a time. Models come
  from the catalog the Slot's CLI reports, with the reasoning efforts each model
  supports.
- **Thinking on/off**: verified custom-API models can turn thinking on or off.
- **CLI and SDK**: `sbx slots` and `client.slots`. `sbx sessions create` accepts
  `--slot`, `--model` and `--effort`. With `--slot`, the Harness now defaults to
  the Slot's provider; it was always Codex before.
- **Console**: "My Cloud Machines" lists and manages Slots, grouped by provider,
  with an "Add machine" menu for Codex, Grok Build and Antigravity.

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
  as official CLIs on your inference key. Antigravity runs as the official `agy`
  CLI on a subscription machine only; Devin is disabled.

Not available yet: preview origins, OAuth acquisition for Connections and a
WebSocket terminal (terminals poll).
