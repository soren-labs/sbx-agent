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
| `sessions create`, `list`, `show`, `send`, `events`, `cancel`, `continue`, `close`, `export` | Sessions and Turns. |
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
