---
title: Cloud machines
description: Run Sessions on your own Codex, Grok Build or Antigravity subscription with Machine Slots - official sign-in, parallel machines, and the models each CLI reports.
---

A **cloud machine** (a Machine Slot in the API) is one independent login to one
provider's official CLI. Three providers are available: **Codex**, **Grok Build**
and **Antigravity**. Use a machine when you want a Session to run on your own
subscription instead of an inference API key.

- SBX never asks for your provider password, never reads or returns the stored
  login, and never substitutes an API key for a subscription.
- The login lives on a private Modal Volume in **your own Modal workspace**.
  SBX stores which Volume it is, never the login itself.
- One machine runs **one Session at a time**. To run three Sessions in
  parallel, add three machines. The same account can be signed in on several
  machines; each login is independent and no machine shares a home directory
  with another.
- Machines add logins, not subscription quota. Your plan's limits still apply.

A verified [Modal Connection](/guides/connections/) is required. Sessions that
use an inference API key are unchanged and need no machine.

## Choose a provider

When you add a machine, pick the provider whose subscription you want to use.
Each choice says how it signs in:

- **Codex** and **Grok Build** use a *device code*: you open a sign-in page and
  enter a one-time code shown by the machine.
- **Antigravity** uses a *Google sign-in link and a pasted code*: you sign in to
  Google in your own browser, copy the code the browser then shows, and paste it
  back into SBX.

The [comparison table](#providers-at-a-glance) below lists the differences.

## Add a machine in the Console

Open **My Cloud Machines** and choose **Add machine**. The menu lists Codex,
Antigravity and Grok Build, each with a one-line hint about how it signs in.
Pick one. SBX starts a temporary setup VM in your Modal workspace and runs that
provider's official login there.

### Grok Build and Codex (device code)

1. The page shows the provider's sign-in address (`accounts.x.ai` for Grok Build,
   `auth.openai.com` for Codex) and a one-time code.
2. Open the address, sign in to the account you want this machine to use, and
   enter the code.
3. The page updates by itself: the CLI confirms the login, SBX makes one small
   real request to prove it works, saves the Volume and removes the setup VM.
   The machine turns **Ready**. There is no upload and no save step.

The code expires after a few minutes. If it expires, the machine stays at
**Needs login** and you can start again. **Cancel login** stops the attempt and
its VM.

### Antigravity (open a link, paste the code)

Antigravity's CLI has no device code. Its sign-in is a Google page instead.

1. The page shows a Google sign-in link (`accounts.google.com`). Open it in your
   own browser and sign in with your Google account. You type your password
   there; SBX never sees it.
2. After sign-in the browser shows a code. Copy it.
3. Back in SBX, paste the code into the field on the machine and choose
   **Send code**. SBX passes the code straight to the waiting CLI in the setup
   VM. It does not store the code.
4. The machine verifies its login and turns **Ready**.

If the code is wrong, already used or too old, the provider does not accept it.
The machine then shows **Needs login** with the reason `code_rejected`. Choose
**Log in again** to get a new link and a new code.

### From the CLI

Device code (Codex or Grok Build):

```bash
sbx slots add --provider grok --label "Grok 1" --wait
```

With `--wait`, the sign-in address and one-time code are printed to stderr.
Without `--wait`, run `sbx slots show SLOT_ID` to read the `login` object, then
`sbx slots wait SLOT_ID` to wait for the outcome.

Pasted code (Antigravity):

```bash
sbx slots add --provider antigravity --label "Antigravity 1" --wait
```

The sign-in address is printed. Open it, sign in, and copy the code the browser
shows. Then send the code in one of these ways:

- In a terminal, `--wait` prompts for the code without echoing it. You can also
  run `sbx slots code SLOT_ID` on its own; it prompts the same way.
- Without a terminal, `--wait` prints the address and the hint and keeps
  waiting. Run `sbx slots code SLOT_ID` from another shell, or pipe one line to
  it: `printf '%s\n' "$CODE" | sbx slots code SLOT_ID`.
- `sbx slots code SLOT_ID --code CODE` also works, but the code then appears in
  your shell history. Prefer the prompt or stdin.

Check the result with `sbx slots show SLOT_ID`. Exit codes are listed in the
[CLI reference](/reference/cli/#exit-codes).

## States

| State | Meaning | What to do |
| --- | --- | --- |
| Ready | Logged in and idle. | Start a Session on it. |
| Running | A Session's VM is using it. | Wait, or pick another machine. |
| Login pending | A login or a check is in progress. | Finish the sign-in and enter the code, or cancel. |
| Needs login | No usable login: never signed in, expired, signed out, or rejected by the provider. Antigravity shows the reason `code_rejected` when the pasted code was not accepted. | **Log in again**. |
| Error | The setup VM was lost or the Volume could not be saved. | **Check login** or **Log in again**. |

Expanding a machine shows its models, the time it was last verified, the CLI
version and the Volume name, with these actions:

| Action | Effect |
| --- | --- |
| Log in again | Runs the official login again on the same Volume. For Antigravity this gives a new link. If you do not finish, a Ready machine stays Ready. |
| Check login and refresh models | Verifies the stored login with one real CLI call on a fresh VM and re-reads the model list. |
| Rename | Changes the label or the account note. The note is your own reminder; SBX does not read account identity from the login. |
| Sign out | Destroys the Volume. The machine stays and needs a login. The provider-side session is not revoked; do that in your provider account if you need to. |
| Delete machine | Removes the machine and the Volume SBX created for it, after you type its name. Refused while a Session is running on it. |

The list is built for many machines: rows are compact, groups collapse by
provider, and you can search, filter by state and sort.

## Run a Session on a machine

In the composer open the **Runs on** picker and choose a machine. Only machines
that can run are listed; a Running one is shown as in use. The model and effort
pickers then come from that machine.

- **Models** are the list the machine's own signed-in CLI reported, with the
  source shown. SBX ships no model list of its own. The three providers differ:
  - **Codex** reports its list with `codex app-server model/list`.
  - **Grok Build** reports its list with `grok agent stdio initialize`.
  - **Antigravity** prints its list with `agy models`. SBX shows each model by
    the name the CLI prints, because the CLI has no machine-readable catalog. If
    SBX cannot read that list, the machine still works, but only with the
    provider's default model, and the model picker is not shown.
  - If the CLI gave no usable list for any provider, no models are offered and the
    provider default is used.
- **Reasoning effort** shows the efforts the selected model lists.
  - For **Codex** and **Grok Build** the choices change with the model, and some
    models have no effort choice at all.
  - **Antigravity** has no separate effort choice. Its CLI lists one model per
    effort level, with the level in the name, for example "(High)" and "(Low)"
    versions of the same model. Pick the version you want. The CLI refuses an
    effort setting next to one of these models, so SBX does not offer one.
- The server refuses a model or effort that is not in the machine's catalog
  (`validation_failed`).

The choice is fixed for the Session: every Turn, retry and resume uses the same
machine, model and effort.

```bash
sbx slots models SLOT_ID
sbx sessions create --slot SLOT_ID --model <model-id> --effort <effort> \
  --message "Fix the failing test."
```

Leave out `--model` and `--effort` to use the provider default. With `--slot`
the Harness follows the machine's provider, so you do not need `--harness`.

A Session on a machine carries no API key and never falls back to one. If the
machine is busy the Turn waits (`waiting_capacity`) and starts when it frees;
no second VM is created for the same machine. The machine is free again when
the Session's compute is released.

If a Turn is rejected because the provider no longer accepts the stored login,
the machine shows **Needs login**. Choose **Log in again**, then send the Turn
again.

## Providers at a glance

| | Codex | Grok Build | Antigravity |
| --- | --- | --- | --- |
| How you sign in | Device code: open the page and enter the one-time code. | Device code: open the page and enter the one-time code. | Open the Google sign-in link, sign in, and paste the code the browser shows. |
| Where sign-in happens | `auth.openai.com`, in your browser | `accounts.x.ai`, in your browser | `accounts.google.com`, in your browser |
| Where models come from | The signed-in `codex` CLI (`codex app-server model/list`). | The signed-in `grok` CLI (`grok agent stdio initialize`). | The signed-in `agy` CLI's list (`agy models`). If it cannot be read, only the provider default is used. |
| Reasoning effort | Per model, as each model lists it. | Per model, as each model lists it. Some models have none. | No separate setting. Each effort level is its own model in the list. |
| Custom API key available | Yes, with an OpenAI Responses endpoint. | Yes, with an OpenAI Chat endpoint. | No. Antigravity runs only on a machine. |

## Thinking control for API-key models

Sessions on an inference API key keep working as before. Where it was measured
to work, the picker offers a thinking **On / Off** switch for the model:

- When an inference Connection is validated, SBX tests per model and protocol
  whether the provider's switch really turns reasoning off.
- The switch is offered only when that test passed **and** the chosen CLI is
  known to forward it. Today that is OpenCode on chat and Anthropic-style
  endpoints. Codex with a custom Responses endpoint does not forward it, so no
  switch is shown there.
- Graded levels are not offered for API-key models. Reasoning effort on a machine
  is a different control and is described above.

## Limits

- Three subscription providers are available: Codex, Grok Build and Antigravity.
  Antigravity runs only on a machine; the other providers also accept an API key.
- A setup or Session VM that dies before its Volume is saved can lose a login
  refresh made in that run; the next check then reports Needs login.
- SBX cannot see or enforce your subscription's own concurrency or usage
  limits. A plan at its usage limit still counts as logged in; Turns fail until
  the provider allows more.
- A Modal Connection cannot be disconnected while machines keep Volumes in it.
