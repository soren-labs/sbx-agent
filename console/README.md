# SBX Browser frontend prototype

Second design pass of the React / TypeScript / Vite Session frontend, based on
hands-on research of authenticated Devin Cloud. Default mode uses explicitly
labeled local demo data for existing SBX capabilities.

```bash
cd console
npm ci
npm run dev -- --host 0.0.0.0 --port 5174 --strictPort
```

Open **http://localhost:5174/**. Dark is the new default; Settings also offers light.

- `/`: compact new-session composer; repository, provider/model, reasoning effort,
  delivery intent; base branch and account behind configuration / Advanced.
- `/sessions`: searchable Sessions with active, finished, and attention filters.
- `/sessions/stream-reconnect`: active coding session; collapsed completed work,
  expanded current work, compact command evidence, follow-up and cancellation.
- `/sessions/event-replay`: completed coding session with a draft PR outcome.
- `/sessions/account-health`: account error, reconnect and retry.
- `/review`: delivered Session list. Session Review records a revision verdict and
  demonstrates the existing review-gated merge flow, with an explicit final action.
- `/integrations`: Connections for multiple subscription accounts, verify,
  credential refresh, secure connect/pair, model catalog refresh, GitHub App sync.
- `/settings`: appearance and shortcuts; connection key in live mode.

Progress, Changes and Review occupy the session's contextual tabs. Per-file diffs
load lazily. Activity uses existing normalized event kinds; tests are command
output, not a new event category. Sidebar pins are local browser preferences.
There is no remote desktop, computer, browser-control, terminal-control, or IDE UI.

New demo sessions replay a representative coding workflow in about 11 seconds.
The seeded active session stays active, replacing the same running build-command
item as output changes. Demo data resets on reload; pins and appearance persist.
All demo pairing, review and merge actions are local simulations; they do not
modify accounts, repositories, or GitHub.

## Authenticated product research

The second pass used the user's dedicated **Windows Chrome CDP 9222** through the
Windows `agent-browser-cdp.cmd` helper, never the old WSL research browser or the
normal Chrome profile. Authentication succeeded.

Actually exercised: authenticated Sessions home and rail; repository mention
picker; model and configuration menus; one existing completed session's header,
settings, transcript and Changes tab; collapse-all and searchable file tree;
Review list and actual PR #98 detail; organization Settings / Connections.

Started one authorized read-only session, **Inspect Session API Organization**
(`28cce8454b504a3b939c1826bce47a65`). Observed startup, initial plan, expanded running
work, shell/file-read evidence, and completion at 2m 46s. It reported no file
modifications. Its final work group collapsed above the summary and idle follow-up
composer. No commit, push, PR, or merge was requested. A distinct queued state was
not observed in Devin; SBX's queue state remains supported by its own backend.

Closely reproduced patterns: compact workspace rail; recent session rows and
local pin action; centered composer with toolbar menus; narrow title/action bar;
roughly 40/60 transcript/context split; plain assistant text interleaved with
“Worked for” disclosures; current command work expanded; bottom follow-up composer;
compact contextual tabs and diff/file navigation; restrained neutral dark/light
colors. SBX's effort/delivery/account controls are adaptations of its own APIs.
Devin-only Ask, voice, automations, Wiki, security profiles and remote tools were
not reproduced. SBX uses its own branding and inline SVG assets.

Locally ignored notes and screenshots are under `.local-research/devin-auth/` at
the worktree root, with OBSERVED and INFERRED sections. They supersede the initial
unauthenticated notes under `.research/devin/`.

## Frontend domain and API

`src/prototype/` holds the shell, composer, workspace, grouped activity, connection
and review facade, demo data and CSS. Existing HTTP normalization remains in
`src/api/`. Product terminology is Sessions, Connections, GitHub, Changes, Review
and Delivery; transport versions never appear in the UI.

- `/v2/sessions*`: creation/list/detail, follow-ups, cancel/retry, SSE, changes,
  lazy file diff and delivery through the retained Session client.
- `/v1`: providers/models, accounts, auth connect/poll/cancel and secure pairing,
  verify/lifecycle refresh, model refresh and GitHub installation/authorize/sync.
- `/v1/tasks/{id}/reviews` and `/merge`: revision review and gated merge. The
  current Session facade uses durable task IDs; the frontend hides that mapping.
  Review refers to the exact snapshot sequence. Existing agent workspace review
  APIs and older operator components remain intact; no remote tooling is added.

Default or `VITE_API_MODE=mock` uses demo mode. `VITE_API_MODE=http` (optional
`VITE_API_BASE`) selects the live client. Management operations retain existing
permissions. Provider credentials are never pasted into the product UI.
Live integration completeness is outside this prototype's validation scope.
No backend runtime, routes, or contract files were modified.

## Lightweight validation

`npm run build` performs TypeScript checking, Vite compilation and build-manifest
creation. Local `agent-browser` checks cover desktop/mobile, menus, Session
progress, commands, changes, PR/review, follow-up, cancellation and Connections.
No new runtime dependencies; no full backend test/acceptance gate; no push or merge.
