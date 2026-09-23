# web/ — sbx-browser console

A build-less single-page console for the public `/v1` API: agents and live
runs, repository workspaces (review pin, publish, merge, handoff), artifacts,
workflows, capacity, and admin pages for accounts, API keys and the GitHub
App. Plain ES modules + CSS — no bundler, no runtime dependencies.

```
index.html  app.js  styles.css  favicon.svg
lib/        api client, SSE reader, router, i18n (en / zh-CN), formatting, UI kit
views/      one module per page (agents, agent, new-agent, workspace, …)
```

## Develop

```bash
make console-dev          # http://127.0.0.1:8790 — real local /v1 plane + fake CLIs
```

The command prints a throwaway API key; paste it into the Connect screen.
Prompts containing `hang`, `slow`, `fail` or `auth` pick the matching fake-CLI
scenario. `GET /__dev/info` returns the demo repository (path, base ref, base
sha) for trying workspaces, git publishing and artifacts.

## How it talks to the control plane

- Auth is `Authorization: Bearer sbx_<key>`. The key is kept in
  `sessionStorage` (or `localStorage` with "Remember on this device") and is
  only sent to the configured control plane.
- Serve the console **same-origin** with `/v1` — the control plane sends no
  CORS headers. `deploy/sbx-edge` does this (static assets + `/v1` proxy); any
  reverse proxy that serves `web/` and forwards `/v1/*` works too.
- SSE is read with `fetch` (EventSource cannot send `Authorization`) and
  resumes with `Last-Event-ID`; the durable run record stays the source of
  truth when a stream ends.
- Optional globals set before `app.js` loads: `window.SBX_API_BASE` (default
  control-plane URL) and `window.SBX_DOCS_URL` (base URL of a hosted
  `docs-site/` build; help links deep-link into it, `zh-cn/` pages for the
  Chinese UI).

## Test

```bash
make test-e2e             # Playwright (config: playwright.config.ts, specs: ../tests/e2e/)
```

Screenshots from the suite land in `tests/e2e/artifacts/`.
