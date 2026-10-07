# Unified Console UI restoration evidence

These are unedited Chromium screenshots of the current branch's production build,
served locally with Vite preview. They use the real App, routes, auth provider,
feature components, typed `/api` client and query/event store. Browser request
interception provides deterministic unified API responses only for this capture;
there is no product mock dependency, mock mode, legacy API or prototype state.
Identities/repositories are fictional and secret-shaped fixture values are
`REDACTED`. Each browser context starts with isolated cookies and web storage.

## Visual references

Studied `83317cd` (split auth, provider cards, responsive fixes), `17f0361` and
`68d1348` (workspace hierarchy and conversation), `5b180ae` (Inter typography and
neutral surfaces), `66dae94` (prompt-first composition, compact rows, mobile
navigation and accessibility) and `38a1fb2` (product Console cutover). Adjacent
`0e43109` screenshots/design notes and `1b456e0` browser workflow work also informed
this restoration. Only visual and interaction ideas were adapted. No historical
client, auth flow, hosted adapter, mock backend or Session state was copied.

## Screenshots

Desktop captures use 1440 × 1000 CSS pixels; mobile captures use 390 × 844.
Desktop pages longer than the viewport use full-page capture. Mobile images show
the actual viewport, including fixed navigation. Fonts are loaded before capture.

| File | Viewport/theme | Verifies |
| --- | --- | --- |
| [01-login-desktop.png](01-login-desktop.png) | Desktop/light | Split brand story and readable sign-in form |
| [02-login-mobile.png](02-login-mobile.png) | Mobile/light | First-class mobile auth, full-width inputs and submit |
| [03-register-mobile.png](03-register-mobile.png) | Mobile/light | Registration and password guidance |
| [04-verify-mobile.png](04-verify-mobile.png) | Mobile/light | Unified email verification completion |
| [05-home-desktop.png](05-home-desktop.png) | Desktop/light | Workspace sidebar, prompt-first composer, recent Sessions |
| [06-sessions-desktop.png](06-sessions-desktop.png) | Desktop/light | Lifecycle filters and compact Session rows |
| [07-session-desktop.png](07-session-desktop.png) | Desktop/light | Conversation hierarchy, tab navigation, separate compute/recovery |
| [08-session-mobile.png](08-session-mobile.png) | Mobile/light | Wrapping Session header, scrollable tabs, readable conversation |
| [09-home-mobile.png](09-home-mobile.png) | Mobile/light | Heading visible after navigation, compact composer, bottom nav |
| [10-connections-mobile.png](10-connections-mobile.png) | Mobile/light | Provider cards and wrapping actions |
| [11-connections-desktop.png](11-connections-desktop.png) | Desktop/light | Provider hierarchy, credential actions, optional Codex form |
| [12-projects-desktop.png](12-projects-desktop.png) | Desktop/light | Versioned repository context and current Project form |
| [13-settings-desktop.png](13-settings-desktop.png) | Desktop/light | Appearance, password and API-key sections |
| [14-settings-dark.png](14-settings-dark.png) | Desktop/dark | Saved theme survives reload; dark form surfaces |
| [15-home-dark.png](15-home-dark.png) | Desktop/dark | Consistent workspace, composer, rows and status colors |

## Interaction verification

[walkthrough.webm](walkthrough.webm) records registration/verification navigation,
fixture login → Home → Sessions → Session conversation/activity → mobile Home →
Connections → Projects → Settings → saved dark theme. It is recorded directly by
Chromium, with no image editing or synthetic rendering.

The capture script asserts rendered auth success, unified login payload, Session
conversation content, activity navigation, disclosure expansion/collapse, visible
model controls, dark theme persistence after reload, absence of page errors and
unexpected API requests, and no horizontal page overflow at 320, 390, 768, 899,
900, 1024 and 1440px. Additional 320px checks cover registration, verification,
Projects, Connections and Settings. Vitest covers protected-route return after
login, registration, token verification, desktop/mobile navigation, scroll reset,
theme persistence/logout, and Ctrl+Enter submission with collapsed options.
Existing event replay, delivery, write-only connection and API tests remain.

The fixture SSE response ends intentionally: Session evidence can display
“Reconnecting to event stream…”. This is the real existing recovery state; the
capture does not simulate a continuously running agent or validate live provider
access. Chromium is the browser checked here; Safari/Firefox and device keyboard
behavior remain P2/P3 follow-up coverage. No known P0/P1 blockers remain.

## Reproduce

From the repository root, use one terminal to serve this checkout's build:

```sh
npm --prefix console ci
npm --prefix console run build
npm --prefix console run preview -- --host 127.0.0.1 --port 5180 --strictPort
```

In another terminal:

```sh
uv run --with playwright==1.63.0 python -m playwright install chromium
uv run --with playwright==1.63.0 python console/scripts/capture-ui.py --url http://127.0.0.1:5180
```

This overwrites the evidence with a fresh real-browser capture. Ensure the server
belongs to this checkout. The script also accepts a Vite development URL.

## Validation

- `make lint`: passed.
- `make test`: 324 passed, 1 skipped; two existing third-party deprecation warnings.
- `make console-check`: typecheck, 41 Vitest tests and production build passed.
- `make docs-check`: build and link checks passed; existing sitemap configuration warning.
- Production-build Chromium walkthrough: passed, including viewport assertions.
