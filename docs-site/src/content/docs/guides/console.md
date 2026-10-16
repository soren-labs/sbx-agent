---
title: Console
description: The SBX web Console - features, how to run it, and the rules it follows.
---

The Console is a React/Vite app in `console/` that talks only to `/api`. It is
not served by `sbx serve`; run it next to the control plane.

```bash
cd console
npm ci
SBX_API_PROXY_TARGET=http://127.0.0.1:8800 npm run dev   # http://localhost:5174
npm run typecheck && npm test && npm run build
```

Set `SBX_PUBLIC_URL` and `SBX_ALLOWED_ORIGINS` on the control plane to the
origin you open in the browser; cookie mutations from other origins are
rejected with `csrf_failed`.

## Pages

- **Home**: the composer for a new Session, with a one-line setup notice until
  the account has an inference API key, Modal and GitHub configured. Pick the coding CLI (OpenCode, Codex,
  Claude Code, Grok Build, Command Code) and a model from the inference
  Connections that CLI can use; it defaults to the first CLI your key can drive,
  that Connection's default model, and Modal once a Modal Connection exists.
  The same picker has **Runs on**: your API key or a ready cloud machine. A
  machine offers the models its own login reported and each model's reasoning
  efforts; an API-key model offers a thinking On/Off switch where verified.
  Antigravity has no API-key mode, so it appears only under **Runs on**. It
  has no separate effort picker: its CLI lists one model per effort level, so
  you choose the level by choosing the model. If SBX cannot read the Antigravity model list, the model
  picker is not shown and the provider default is used.
- **Session workbench**: the conversation stays on the left while the agent
  works. With Claude Code and Grok Build the reply and any reasoning the model
  exposes appear as they are generated; the other CLIs report each completed
  step. Tool calls are grouped into "Working for…"/"Worked for…" blocks: runs of
  file reads, searches and edits fold into one line, and any line expands to the
  exact command or path with its input and output. The status bar above the
  composer says what the agent is doing right now (the running command,
  thinking, writing, or waiting for the model) with Stop; a finished Turn shows
  its duration and tokens, or the failure with Retry where the server allows it. The workspace panel on the right (or the
  **Workspace** switch on small screens) holds Overview, Changes, Files,
  Terminal, Services, Children and Activity.
- **Sessions**: the list and the Session workbench — the conversation with the
  live work log on the left and a workspace panel (Overview, Changes, Files,
  Terminal, Services, Children, Activity) on the right. Visitors who are not
  signed in see the product landing page at `/` instead.
- **Projects**: reusable repository and environment configuration. **Publish
  new version** starts from the current version and changes only what you edit
  (repository, base ref, agent CLI, model, compute, checks, services); settings
  the form does not show, such as environment variables, reasoning effort and
  the delivery policy, are kept and listed. Before publishing, the dialog lists
  every changed field, and a change to the delivery policy must be confirmed.
  Checks are one per line as `name=command`; services have a name, a command
  and an optional port. Commands are split like a POSIX shell
  (`smoke=python3 -c "print(123)"` runs `python3`, `-c`, `print(123)`), but no
  shell runs and nothing is expanded: write `sh -c '…'` yourself for pipes or
  `&&`. The dialog shows the exact arguments of every command.
- **Machines**: subscription logins, one per machine, for Codex, Grok
  Build and Antigravity. Choose **Add machine** and pick a provider from the menu;
  each entry says how it signs in. For Codex and Grok Build, open the sign-in
  page and enter the one-time code. For Antigravity, open the Google sign-in link,
  sign in, copy the code the browser shows, paste it into the machine's field and
  choose **Send code**. SBX does not store the code. The row turns Ready by itself.
  Machines are grouped by provider, with compact rows, search, state filter, sort
  and collapsible groups. Expand one to log in again, check the login, rename, sign
  out or delete. See [Cloud machines](/guides/cloud-machines/).
- **Connections**: add, validate, replace and disconnect, with write-only
  secret fields.
- **Settings**: identity, API keys (plaintext shown once) and preferences.

## Rules the UI follows

- It renders server fields and never re-derives turn outcomes, connection
  health, merge eligibility or review validity. Merge sends the server's pins.
- One store holds the snapshot and its watermark, replays events by sequence,
  dedupes by `seq` and resyncs on gaps.
- Secrets never enter web storage; caches are purged on logout.
- Compute is never started implicitly. When the executor is unavailable, Files,
  Terminal and Services show a diagnosis instead.
- Retry buttons name what they retry: Turn, Delivery or validation.

## Services and delivery

- **Services** lists the services declared by the Project version a Session
  started from; processes the agent or a terminal starts on their own are not
  listed. A Session without a Project has none. Start, Stop and Logs act on the
  Session's compute; while compute is not running, Start records the wish and
  the service starts when the Session next runs.
- **Changes → Deliveries** shows where a delivery would go (for example "Opens a
  draft pull request on owner/repo against main"). When delivery is not possible
  it says why instead of showing a form: a Session without a repository offers
  **Download patch** (apply it with `git apply --binary`), and a missing or
  revoked GitHub Connection links to Connections.

## Limits

The Terminal polls for output. Service preview is not available because no
preview origin is configured; a service declared with a preview says so.
## Workspace files and terminals

- **Open or create file** opens an empty editor for a path that does not exist
  yet. Saving creates it, and fails with a conflict if someone else created the
  file in the meantime.
- Files larger than 1,000,000 bytes, and files whose credential values are
  hidden, open as read-only previews. Saving a preview would cut the file short
  or overwrite the hidden values, so these files are changed from the terminal
  or by the agent.
- The Terminal is a command console. It shows colours and progress lines, and
  hides control sequences. Switching panels does not end it. Each terminal can be
  closed explicitly; **New terminal** starts another. Terminals end when the
  Session's compute is released.
- If the connection drops before a command is confirmed, the Terminal says so
  and offers **Retry same command**. The retry reuses the request key, so the
  command runs at most once.
- **Live changes** observes the workspace again when you open it, every few
  seconds while it is visible, after each Turn, and on **Refresh**.

## Limits

The Terminal polls for output and sends whole lines. Service preview is not
available because no preview origin is configured.
