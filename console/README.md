# Session Console (SOR-257, V2 first pass)

React + TypeScript + Vite implementation of the V2 Session Console. Frontend
only — the API client is isolated so it swaps from typed fixtures to the real
control plane (SOR-256) without touching UI semantics.

## Layout

```
src/
  api/          contract types, normalization, client impls
    types.ts      product vocabulary: Session / Turn / Activity / Change
    normalize.ts  backend (run≙turn) → product mapping
    client.ts     SessionApi interface + ApiError
    fixtures.ts   typed fixtures (REDUCTED-safe; no real credentials)
    mock.ts       FixtureSessionApi — in-memory + scripted event streams
    http.ts       HttpSessionApi — V2 /v2/sessions + V1 providers/models/GitHub
                  (route table at top)
  components/   shell, composer, conversation/activity, follow-up, error UX
  pages/        New Session (/), Sessions, Session detail, Integrations, Settings
  i18n/         en + zh-CN dictionaries
  theme/        light/dark/system
  __tests__/    vitest + testing-library
```

## Commands

```bash
npm install
npm run dev        # vite dev server on :5174
npm test           # vitest
npm run typecheck  # tsc --noEmit
npm run build      # typecheck + production build → dist/
```

## API mode

- `VITE_API_MODE=mock` (default in dev): fixture-backed, fully offline.
- `VITE_API_MODE=http` + `VITE_API_BASE=<control plane>`: live API —
  `/v2/sessions*` for session lifecycle and `/v1/providers`, `/v1/models`,
  `/v1/github/app*` for provider/account/GitHub surfaces (bearer token from
  Settings → API token, stored in localStorage only).

SOR-262 integration notes: `src/api/http.ts` holds the entire route table and
backend→product mapping — retarget session-named routes there only. The dev
server proxies `/v2`, `/v1` and `/api` to `127.0.0.1:8787` (vite.config.ts)
for local control-plane development.

## Product rules encoded

- IA is exactly New Session / Sessions / Integrations / Settings; `/` is the
  composer + recent sessions (not a dashboard). Tasks/Agents/Workflows/
  Artifacts never appear as top-level concepts.
- Session page: human meta (title/status/provider/model/repo), Conversation +
  normalized Activity timeline, sticky follow-up composer, conditional Changes
  tab, compact Details/Usage/Runtime rail. Run/Agent/Revision are not surfaced.
- Composer: prompt, repo, Auto provider/model, Send; effort/delivery/account/
  compute/secrets/MCP/idle-timeout live behind Advanced. No LRU/scheduler/
  Modal/raw account ids.
- Error UX maps canonical subcodes to actionable product states (provider
  login, busy, runtime disabled, GitHub required, session failed, reconnect).
