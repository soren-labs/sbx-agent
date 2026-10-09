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
  server order — prose as Markdown/GFM, consecutive `tool`/`reasoning` parts as one work group
  ("Working"/"Worked" with counts by kind). Tool names from all five CLIs are classified
  (command, edit, read, search, web, plan, agent); unknown tools stay generic. Each step expands
  to its input (readable arguments, not escaped JSON) and output, bounded to 14 lines with an
  exact "show N more lines". A tool is shown running only while its Turn is live.
* **Turn status**: one line per Turn from server state — queued, starting (`preparing`,
  `waiting_capacity`), working with elapsed time and Stop, completed with real duration and
  reported tokens, or a failure/cancel/interrupt card with the error code, a plain explanation,
  and Retry/Acknowledge when the server offers the action. No cost, effort or PR status is
  derived client-side.
* **Scroll**: the log follows new output only while the reader is within 80 px of the end;
  otherwise position is kept and "Jump to latest" appears. The composer is docked, grows to
  220 px, sends on Enter (Shift+Enter breaks the line, IME-safe) and queues behind a live Turn.
* **Workspace panel** (`/sessions/{id}/{overview|changes|files|terminal|services|children|activity}`):
  beside the conversation on wide screens; below 1100 px a Conversation/Workspace switch shows
  one pane. Activity is a worded timeline (event type on hover; per-chunk updates behind a toggle).
* **Connection loss**: the event stream is presumed dead after 40 s without events or server
  heartbeats, or immediately on the browser's `offline` event. The header shows a reconnecting
  banner; on recovery the stream replays after the last applied `seq` and projections refetch.

Current browser evidence is documented in
[Console UI restoration notes](../../../console/docs/ui-restoration/notes.md).
