---
title: Cloud machines
description: Run Sessions on your own Codex subscription with Machine Slots - official login, parallel machines, real models and reasoning effort.
---

A **cloud machine** (a Machine Slot in the API) is one independent official
subscription login. Today the only provider is **Codex**. Use it when you want a
Session to run on your Codex subscription instead of an inference API key.

- The login lives on a private Modal Volume in **your own Modal workspace**.
  SBX stores which Volume it is, never the login itself.
- One machine runs **one Session at a time**. To run three Sessions in
  parallel, add three machines. The same account can be logged in on several
  machines; each login is independent.
- Machines add logins, not subscription quota. Your plan's limits still apply.

A verified [Modal Connection](/guides/connections/) is required. Sessions that
use an inference API key are unchanged and need no machine.

## Add a machine

In the Console open **My Cloud Machines** and choose **Add Codex machine**.

1. SBX starts a temporary setup VM in your Modal workspace and runs the official
   Codex CLI's device login there.
2. The page shows the official sign-in URL and a one-time code. Open the page,
   sign in to the account you want this machine to use, and enter the code.
3. The page updates by itself: the CLI confirms the login, SBX makes one small
   real request to prove it works, saves the Volume and removes the setup VM.
   The machine turns **Ready**. There is no upload and no save step.

The code expires after a few minutes; an unused code leaves the machine at
**Needs login** and you can start again. **Cancel login** stops the attempt and
its VM.

From the CLI the same flow is:

```bash
sbx slots add --label "Codex 1" --wait     # URL and code are printed to stderr
sbx slots list
```

## States

| State | Meaning | What to do |
| --- | --- | --- |
| Ready | Logged in and idle. | Start a Session on it. |
| Running | A Session's VM is using it. | Wait, or pick another machine. |
| Login pending | A login or a check is in progress. | Approve the code, or cancel. |
| Needs login | No usable login: never approved, expired, signed out, or rejected by the provider. | **Log in again**. |
| Error | The setup VM was lost or the Volume could not be saved. | **Check login** or **Log in again**. |

Expanding a machine shows its models, the time it was last verified, the CLI
version and the Volume name, with these actions:

| Action | Effect |
| --- | --- |
| Log in again | Runs the official login again on the same Volume. If you do not finish, a Ready machine stays Ready. |
| Check login and refresh models | Verifies the stored login with one real CLI call on a fresh VM and re-reads the model list. |
| Rename | Changes the label or the account note. The note is your own reminder; SBX does not read account identity from the login. |
| Sign out | Destroys the Volume. The machine stays and needs a login. The provider-side session is not revoked; do that in your provider account if you need to. |
| Delete machine | Removes the machine and the Volume SBX created for it, after you type its name. Refused while a Session is running on it. |

The list is built for many machines: rows are compact, groups collapse, and you
can search, filter by state and sort.

## Run a Session on a machine

In the composer open the model picker and choose a machine under **Runs on**.
Only machines that can run are listed; a Running one is shown as in use.

- **Models** are the list that machine's own logged-in CLI reported
  (`codex app-server`, `model/list`), with the source shown. SBX ships no model
  list of its own. If the CLI gave no list, no models are offered and the
  provider default is used.
- **Reasoning effort** shows only the levels the selected model lists. Models
  differ, so the choices change with the model. The server refuses a model or
  effort that is not in the catalog (`validation_failed`).

The choice is fixed for the Session: every Turn, retry and resume uses the same
machine, model and effort.

```bash
sbx slots models SLOT_ID
sbx sessions create --slot SLOT_ID --model MODEL_ID --effort EFFORT \
  --message "Fix the failing test."
```

A Session on a machine carries no API key and never falls back to one. If the
machine is busy the Turn waits (`waiting_capacity`) and starts when it frees;
no second VM is created for the same machine. The machine is free again when
the Session's compute is released.

## Thinking control for API-key models

Sessions on an inference API key keep working as before. Where it was measured
to work, the picker offers a thinking **On / Off** switch for the model:

- When an inference Connection is validated, SBX tests per model and protocol
  whether the provider's switch really turns reasoning off.
- The switch is offered only when that test passed **and** the chosen CLI is
  known to forward it. Today that is OpenCode on chat and Anthropic-style
  endpoints. Codex with a custom Responses endpoint does not forward it, so no
  switch is shown there.
- Graded levels are not offered for API-key models.

## Limits

- Codex is the only subscription provider.
- A setup or Session VM that dies before its Volume is saved can lose a login
  refresh made in that run; the next check then reports Needs login.
- SBX cannot see or enforce your subscription's own concurrency or usage
  limits. A plan at its usage limit still counts as logged in; Turns fail until
  the provider allows more.
- A Modal Connection cannot be disconnected while machines keep Volumes in it.
