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

Current browser evidence is documented in
[Console UI restoration notes](../../../console/docs/ui-restoration/notes.md).
