---
title: CLI
description: The sbx command - client commands over /api and the operator commands serve and migrate.
---

The exact generated help for every command is published as
[cli-help.txt](/cli-help.txt). Client defaults are listed in
[config-reference.json](/config-reference.json).

## Client configuration

| Field | Environment | Default |
| --- | --- | --- |
| `base_url` | `SBX_BASE_URL` (or `--base-url`) | `http://127.0.0.1:8800` |
| `api_key` | `SBX_API_KEY` | none; stored by `sbx auth login` |
| `workspace_id` | `SBX_WORKSPACE_ID` | first workspace of the principal |

`sbx auth login --email you@example.com` reads the password from stdin or a
prompt, signs in, mints an API key and writes it to
`$XDG_CONFIG_HOME/sbx/client.json` (default `~/.config/sbx/client.json`) with
mode `0600`. Environment variables override the file.

## Commands

| Command | Purpose |
| --- | --- |
| `auth login` | Sign in and store an API key. |
| `projects list`, `projects create SLUG --spec-file F` | Projects and ProjectVersions. |
| `connections list`, `add KIND`, `replace ID`, `validate ID`, `disconnect ID`, `show ID` | Connections. Kinds: `modal`, `github`, `inference_api`. Secrets come from stdin or `--credential-file`, never argv; `inference_api` takes `--endpoint PROTOCOL=BASE_URL` (repeatable) and `--model`. |
| `slots list`, `providers`, `add`, `login`, `verify`, `wait`, `show`, `models`, `cancel-login`, `logout`, `rename`, `delete` | Machine Slots: subscription logins. See [Machine Slots](#machine-slots). |
| `sessions create`, `list`, `show`, `send`, `events`, `cancel`, `continue`, `close`, `export` | Sessions and Turns. `create` accepts `--slot`, `--model` and `--effort` for Machine Slots. |
| `execute PROMPT [--session ID] [--project ID]` | Create or continue a Session and follow the Turn. |
| `changesets capture`, `apply`, `show` | ChangeSets. `capture --salvage` marks a salvage capture. |
| `deliveries request`, `retry`, `show`, `merge` | Deliveries and gated merge. |
| `delegations spawn`, `wait`, `result`, `cancel` | Child Sessions. Roles: `review`, `test`, `research`, `security`, `integration`. |
| `operations show ID` | Long-operation status. |
| `serve [--host H] [--port P]` | Run the control plane and workers (operator). |
| `migrate` | Apply database migrations (operator). |

Output is JSON. Errors print a JSON object with `code`, `message` and `action`
to stderr and exit with status 1.

`serve` and `migrate` read the control-plane variables described in
[Configuration](/self-hosting/configuration/); they are equivalent to
`python -m control.composition serve` and `migrate`.

## Machine Slots

A Machine Slot is one independent official Codex subscription login. Its
credentials live on a private Modal Volume in your own Modal workspace, and one
Slot runs one Session at a time. See [Cloud machines](/guides/cloud-machines/)
for the concept.

| Command | Arguments and flags | Effect |
| --- | --- | --- |
| `slots list` | none | Slots with summary counts and the available providers. |
| `slots providers` | none | Subscription providers this deployment offers. |
| `slots add` | `--provider NAME` (default `codex`), `--label TEXT`, `--account-alias TEXT`, `--volume NAME`, `--connection ID`, `--wait`, `--timeout SECONDS` (default `1200`) | Create a Slot and start its official login. `--volume` adopts an existing Modal Volume and only verifies it; SBX never deletes that Volume. `--connection` selects the Modal connection (default: the verified one). |
| `slots login SLOT_ID` | `--wait`, `--timeout SECONDS` (default `1200`) | Run the official login again. |
| `slots verify SLOT_ID` | `--wait`, `--timeout SECONDS` (default `1200`) | Re-check the stored login with one real CLI call and refresh the model catalog. |
| `slots wait SLOT_ID` | `--timeout SECONDS` (default `1200`) | Wait until no login is pending. |
| `slots show SLOT_ID` | none | One Slot. |
| `slots models SLOT_ID` | none | The model catalog and each model's reasoning efforts, with its `source`. |
| `slots cancel-login SLOT_ID` | none | Stop the pending login and its VM. |
| `slots logout SLOT_ID` | none | Destroy the stored login. The Slot stays and becomes `needs_login`. |
| `slots rename SLOT_ID` | `--label TEXT`, `--account-alias TEXT` | Change the label or the account alias. |
| `slots delete SLOT_ID` | `--confirm LABEL` (required) | Delete the Slot and the Volume SBX created for it (an adopted Volume is kept). `--confirm` must repeat the Slot label. |

`--wait` makes `add`, `login` and `verify` block until the login settles, using
the same rules as `slots wait`. Without `--wait` they return the Slot at once.

### Sign-in prompt

While a command waits, the sign-in URL and one-time code are printed to
**stderr**, once, when the login is waiting for your approval:

```text
Open <verification URL> and enter the code <code> (expires <time>).
```

Stdout stays machine-readable JSON, so `2>/dev/null` keeps only the JSON. The
code is never written to stdout. Without `--wait` nothing is printed; the URL
and code are in the `login` object returned by `slots show` until the code is
used.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | OK. |
| `1` | API error. A JSON error object is written to stderr. |
| `2` | The Slot is not ready after waiting, for example `needs_login`, `error` or `deleting`. The Slot JSON is written to stdout. |
| `3` | The login is still pending when `--timeout` expires. The Slot JSON, with `status` `login_pending`, is written to stdout. |

Argument errors are reported by the argument parser and also exit with `2`;
they write no JSON to stdout.

### Sessions on a Slot

`sbx sessions create` takes three options for Machine Slots:

- `--slot SLOT_ID` runs the Session on that Slot instead of an inference API
  key. The executor backend defaults to `modal` unless `--backend` is given.
  `--harness` defaults to `opencode`, and with `--slot` that default is sent as
  `codex`. Any other `--harness` value is sent as given.
- `--model MODEL_ID` selects a model from `sbx slots models`. Without it the
  provider default is used.
- `--effort LEVEL` selects a reasoning effort that the chosen model lists. An
  unsupported model or effort is rejected by the server with
  `validation_failed` and exit status `1`.

```bash
sbx sessions create --slot SLOT_ID --model MODEL_ID --effort EFFORT \
  --message "Summarize this repository."
```
