# SBX Agent Console

React 18 / TypeScript / Vite web Console for the unified `/api/...` business API
(see `docs/architecture/unified/08-api-console-sdk.md`). It talks only to `/api`;
there is no `/v1`, `/v2`, `/hosted` or mock backend.

```bash
cd console
npm ci
SBX_API_PROXY_TARGET=http://127.0.0.1:8800 npm run dev   # http://localhost:5174
npm run typecheck && npm test && npm run build
```

## Layout

- `src/api/` – the one typed client: `http.ts` (CSRF header, per-mutation
  `Idempotency-Key`, same-key retry on uncertain outcomes), `errors.ts`
  (canonical `error{code,…}` mapping), `sse.ts` (event-stream parser), `client.ts`
  (resource namespaces), `types.ts`.
- `src/state/` – the one query/event store: `query.ts` (bounded in-memory cache,
  scoped by user/workspace, purged on logout), `session-events.ts` (pure reducer:
  seq/id dedupe, revisioned part replacement, gap ⇒ resync), `session-live.ts`
  (snapshot + watermark, SSE replay, reconnect without side effects),
  `auth.tsx`, `context.tsx`.
- `src/features/` – auth, setup checklist, home/composer, sessions list, session
  view (conversation, activity, changes + deliveries, files, terminal, services,
  child sessions), projects, connections, settings.
- `src/i18n`, `src/theme`, `src/styles.css`, `src/features/features.css` – English
  (+ partial 简体中文), light/dark, responsive layout.

## Rules the UI follows

- Server fields are rendered, never re-derived: turn outcome, connection health,
  merge eligibility and review validity come from the API. Merge sends exactly the
  pins in `merge_eligibility` (+ delivery `version`).
- Secrets are write-only: password inputs, cleared on submit, never written to
  web storage. API-key plaintext is shown once and then discarded.
- Availability (lease/worktree/recovery point) is shown apart from conversation
  outcome. Retry buttons name what is retried (Turn, Delivery, validation).
- Compute is never started implicitly; `executor_unavailable` renders a diagnosis.
- Session composer defaults to harness `opencode`, the preferred free model from
  `/api/models`, and Modal when a Modal connection exists. Codex is optional.
