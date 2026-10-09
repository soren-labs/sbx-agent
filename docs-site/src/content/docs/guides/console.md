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

- **Home**: the setup checklist (account, inference API key, Modal, GitHub)
  and the composer for a new Session. Pick the coding CLI (OpenCode, Codex,
  Claude Code, Grok Build, Command Code) and a model from the inference
  Connections that CLI can use; it defaults to the first CLI your key can drive,
  that Connection's default model, and Modal once a Modal Connection exists.
- **Session workbench**: the conversation stays on the left while the agent
  works. Tool calls are grouped into "Working"/"Worked" blocks you can expand to
  see each command, file edit and its output; the status line shows whether the
  Turn is queued, starting, working, completed or failed, with Stop and Retry
  where the server allows them. The workspace panel on the right (or the
  **Workspace** switch on small screens) holds Overview, Changes, Files,
  Terminal, Services, Child Sessions and Activity.
- **Sessions**: the list and the Session view with Conversation, Activity,
  Changes (ChangeSets, Deliveries, merge eligibility), Files, Terminal,
  Services and Child Sessions tabs.
- **Projects**: reusable repository and environment configuration.
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

## Limits

The Terminal polls for output. Service preview is not available because no
preview origin is configured.
