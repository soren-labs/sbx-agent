# SOR-267 — Session Console UI/UX polish: design rationale

Second-pass ("production-grade") polish of the Session Console on top of the
SOR-257 first pass. Goal: a Devin-style Session-first IA — the only top-level
objects a user sees are **New Session · Sessions · Integrations · Settings**.

## Information architecture

- **Home = New Session.** A narrow, single-column prompt-first surface
  (`.narrow`, 760px): the prompt textarea is autofocused, provider/model
  default to *Auto*, advanced options stay behind the `Advanced` disclosure.
  Repo is optional. Recent sessions sit below the composer in a card strip —
  a link back into work, not a dashboard.
- **Sessions index = one row per session.** The card grid was replaced with a
  scannable row list (`.session-row`): status pill, title + last-activity
  preview, provider/model, repo, relative time. Search and All/Live/Ended
  filters live in a page head that wraps cleanly on phones (full-width search
  under 560px — fixes a clipped heading found while dogfooding 390px).
- **Session detail.** Back link → All sessions; title + status pill + a compact
  meta row that also hosts the contextual actions (Retry on failed, Create PR
  when changes exist, Open PR once delivered). Conversation is the primary
  tab; Activity is the raw normalized stream; Changes only renders when the
  session reports real changes. Details / Usage / Runtime collapse into a
  sticky right rail ≥1100px, inline below the conversation on smaller screens.

## Interaction

- **Send < 100 ms.** Create navigates immediately with an optimistic session
  shell (queued/starting phase banner); measured send→shell ≈ 80 ms against
  the real control plane.
- **Keyboard.** Ctrl/Cmd+Enter submits from the prompt and the follow-up
  composer; a hint sits beside Send (hidden on touch widths). Skip-to-content
  link; visible `:focus-visible` outlines; `prefers-reduced-motion` kills
  animation.
- **Live states.** Turns show "Working…" with a spinner and "Queued — will
  start shortly" while pending; activity rows carry right-aligned timestamps;
  command items keep their exit code plus an expandable `output` block that
  was previously dropped. The sticky follow-up composer clears the fixed
  mobile bottom-nav (was obscured on first pass).
- **Failure UX, not 500s.** `ErrorNotice` gains a `secondary` escape route:
  provider-sourced session failures offer *Open Integrations* beside Retry;
  runtime-disabled now links to Integrations (where the Runtime card lives)
  instead of Settings; per-activity provider errors link to Integrations
  inline. Provider cards on Integrations surface `connectionDetail` and a
  needs-login hint.

## Evidence (real `/v2` session, `serve_console.py` + vite proxy, token via Settings)

| shot | surface |
| --- | --- |
| `01-home-1440-light-en.png` | New Session, 1440×900 light en |
| `02/03` | live session — created via UI, idle after success, 1440×900 |
| `04` | Activity tab — normalized stream |
| `06` | follow-up turn in-flight |
| `07/08` | Sessions rows / Integrations cards, 1440×900 |
| `09/10` | dark theme, 1440×900 |
| `11–13` | zh-CN, 1280×800 |
| `14–16` | mobile 390×844 — home, sessions, session + follow-up above bottom nav |
| `17` | mobile dark zh-CN |

## Notes / gaps surfaced (not fixed here — out of console scope)

- `session.meta`/usage rows arrive but `delivery`-level PR create depends on a
  GitHub App install (Integrations shows it as not configured locally).
- Changes tab correctly stayed hidden in dogfood — the fake provider produced
  no workspace diff.
