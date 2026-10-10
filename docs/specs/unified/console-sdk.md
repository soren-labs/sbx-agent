# One API, one Console state model, one SDK

* **API**: `docs/specs/unified/openapi.yaml` is generated from `control/api` by
  `scripts/export_openapi.py`; `tests/unit/test_openapi_drift.py` fails on drift or on any
  `/v1`, `/v2` or `/hosted` path. Live surfaces (files, terminal, services) report
  `executor_unavailable` instead of waking compute. Terminals are nested under the Session
  (`/api/sessions/{s}/terminals/{t}/input|output`) and poll for output; this is a documented
  deviation from a WebSocket attach.
* **Console** (`console/src`): one typed client (`api/client.ts`), which sends CSRF and
  `Idempotency-Key` headers and reuses the key on retry. One store (`state/`) holds the initial
  snapshot and watermark, replays events by sequence, dedupes by seq, applies part revisions by
  replacement, and resyncs on gaps. Features are Projects, Sessions, Conversation, Activity, Changes
  (with server merge eligibility and pins), Files, Terminal, Services, Child Sessions, Connections
  (write-only secret inputs, cleared after submit, never stored in web storage), Settings and the
  setup checklist (email/password + inference API key + Modal + GitHub). Session creation
  picks the Harness (official CLI) and a model from the inference Connections that Harness can
  use; nothing is offered that the API did not report.
* **SDK** (`src/sbx/sdk`): namespaces `projects`, `connections`, `sessions`, `messages`, `turns`,
  `changesets`, `deliveries`, `delegations` and `operations`; `connections.add_inference()` adds a
  BYOK key and `harnesses()`/`models(provider_id)` expose the catalog. `execute()` creates or continues
  ordinary resources and follows committed events until the Turn is terminal. `turns.wait` and
  `delegations.wait_result` are distinct. Uncertain transport retries reuse the Idempotency-Key, and
  an unknown outcome raises `OutcomeUnknown`.
* **CLI** (`sbx`): `auth login` reads the password from stdin or a prompt and stores an API key in a
  0600 config file. `connections add/replace` read secrets from stdin or a file, never argv;
  `inference_api` settings are flags (`--endpoint PROTOCOL=BASE_URL`, `--model`). `sessions create
  --harness` selects the CLI and `harnesses` lists manifests. Also
  provides `sessions …`, `execute`, `changesets`, `deliveries`, `delegations`, `operations`, and the
  operator commands `serve` and `migrate`.

## Console presentation

The Console shares typography and surface tokens across Home, Projects, Sessions,
Connections, Settings and auth. It supports saved light/dark themes and system
preference on both anonymous and authenticated pages, a desktop workspace sidebar,
and mobile top bar/bottom navigation. Route changes reset page scroll. Home's
**Session options** disclosure retains project/repository, model and executor
selections while collapsed; task submission and Ctrl/⌘+Enter use the existing
Session creation command. This presentation does not change routes, request
schemas, server action eligibility, identity, or the query/event store.

### Session workbench

The Session view is a two-pane workbench, not a tab that replaces the conversation:

* **Conversation** (`role="log"`): Messages grouped under their Turn. Assistant parts render in
  server order — prose as Markdown/GFM, consecutive `tool`/`reasoning` parts as one work group.
  The group header is "Working for <elapsed>" while its Turn is live and "Worked for <duration>"
  afterwards, measured from the parts' `created_at`/`updated_at` (no duration is shown for parts
  that never recorded them), followed by counts by kind and the number of failed steps. A group
  is open while the agent works in it and collapses when it finishes; the reader's own toggle
  always wins and survives new output.
* **Rows and folding**: tool names from all five CLIs are classified (command, edit, read,
  search, web, plan, agent); unknown tools stay generic. Consecutive *finished* steps of the same
  quiet kind (read, search, edit, web, plan, repeats of one generic tool) fold into one row
  ("Read 9 files · a.py, b.py, …") that expands to every step. Commands, a running step and a
  failed step always keep their own row. A step expands to the exact command or path, its
  input (readable arguments, not escaped JSON) and output, bounded to 14 lines with an exact
  "show N more lines". While a group is live only its newest 8 rows are shown, with "Show N
  earlier steps"; once the reader clicks inside the list, no further row leaves it. A tool is
  shown running only while its Turn is live.
* **Narration**: reply text the agent writes *between* two tool calls of one message is a
  quiet one-line row of that work group (first line as plain words; it expands to the full
  Markdown), so a long run stays one group. Text before the first tool call and after the
  last one stays in the reply itself.
* **Streaming**: `message.part_added`/`message.part_updated` events with `mode: "append"` grow a
  text or reasoning part in place. A part is presented as streaming (caret, "Thinking" opened,
  "Writing the reply") only while this browser received a change to it in the last 2.5 s and
  it is the newest unsealed part of a live Turn; whole blocks from CLIs that do not stream, and
  parts loaded from a snapshot, are never animated. Reasoning shows "Thought for <time>" from
  recorded part times once it is complete.
* **Turn status**: a live Turn has one status bar docked above the composer, from server state —
  queued, starting (`preparing`, `waiting_capacity`), or running with what the newest part says
  is happening (the running tool and its command/path, "Thinking", "Writing the reply", or
  "Waiting for the model"), elapsed time and Stop. A finished Turn shows completed with real
  duration and reported tokens, or a failure/cancel/interrupt card with the error code, a plain
  explanation, and Retry/Acknowledge when the server offers the action. No cost, effort or PR
  status is derived client-side.
* **Scroll and composer**: the log follows new output only while the reader is within 80 px of
  the end; otherwise position is kept and a "Jump to latest" button appears. The composer is
  docked, grows to 220 px, sends on Enter (Shift+Enter breaks the line, Escape leaves the box,
  IME-safe) and queues behind a live Turn. A sent message appears in the log immediately as
  "sending" and is replaced by the server's Message once accepted; if the request fails the
  text returns to the box with a retry that reuses the same idempotency key.
* **Stream trace** (diagnostic, off by default): with `localStorage["sbx.streamTrace"] = "1"`
  each part/tool event records runtime `observed_at`, control `recorded_at`, browser receipt
  and paint times in `window.__sbxStreamTrace`.
* **Workspace panel** (`/sessions/{id}/{overview|changes|files|terminal|services|children|activity}`):
  beside the conversation on wide screens; below 1100 px a Conversation/Workspace switch shows
  one pane. Activity is a worded timeline (event type on hover; per-chunk updates behind a toggle).
* **Connection loss**: the event stream is presumed dead after 40 s without events or server
  heartbeats, or immediately on the browser's `offline` event. The header shows a reconnecting
  banner; on recovery the stream replays after the last applied `seq` and projections refetch.

Current browser evidence is documented in
[Console UI restoration notes](../../../console/docs/ui-restoration/notes.md).
