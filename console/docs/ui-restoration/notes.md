# Opus 5.5 Visual Restoration Evidence & Notes

These are unedited Chromium screenshots and a full interaction video walkthrough
captured against the production build of `ui/restore-polished-console` (PR #193),
served locally via Vite preview.

The visual restoration faithfully reconstructs the Opus 5.5 polished product UI,
using the authoritative visual gold references and historical commit `83317cd8b90b487a01a534ac7c440154efac2d03`
(along with adjacent PRs #163, #145, #114).

All unified backend/API contracts, typed `/api` client, auth provider, workspace/session/project/connection
models, routing semantics, SSE, and tests are preserved with zero legacy `/v1`/`/v2` or mock dependencies.

---

## Authoritative Gold Reference Mapping

| Reference | Page | Implementation Surface | Verified By |
| --- | --- | --- | --- |
| **Image(8)** (`gold-login.png`) | Opus 5.5 Login / Auth Gate | 50/50 dark split layout, navy-black glowing grid hero, live preview task card, password reveal affordance, full-width white CTA | `01-login-desktop.png`, `02-login-mobile.png`, `03-register-mobile.png`, `04-verify-mobile.png` |
| **Image(9)** (`gold-home.png`) | Opus 5.5 Home Workspace | Persistent dark sidebar (New session `Ctrl 0`, Search `Ctrl K`, Review count, Pinned/Recent sessions, Connections & Settings, identity footer), centered greeting hero (`Good morning, alex`), setup progress ring (3/3), prompt-first composer with repo/options/model tool chips, circular send button, shortcut hint (`Ctrl Enter`), quick action chips, and 2x2 recent sessions grid | `05-home-desktop.png`, `09-home-mobile.png`, `15-home-dark.png` |
| **Image(10)** (`gold-connections.png`) | Opus 5.5 Integrations | Persistent sidebar shell, "Integrations" lead, 3 numbered provider cards: 1 Modal (status pill, runtime version, checklist), 2 GitHub (status pill, installation box with avatar, repo list, disconnect), 3 Codex / OpenCode Zen (status pill, model & free note, action buttons) | `10-connections-mobile.png`, `11-connections-desktop.png` |
| `opus-session-history.png` | Opus 5.5 Session Workspace | Flat dark workspace surfaces, title/status/harness chips, Conversation/Activity/Changes/Files/Terminal/Services tabs, followup composer, and right rails (Details, Usage, Runtime, Compute & recovery) | `07-session-desktop.png`, `08-session-mobile.png` |
| `opus-home-mobile-history.png` | Opus 5.5 Mobile Navigation | Mobile drawer, responsive header, compact composer chips, and clean mobile bottom navigation | `02-login-mobile.png`, `08-session-mobile.png`, `09-home-mobile.png`, `10-connections-mobile.png` |

---

## Evidence Manifest

Desktop captures use **1540 × 960** CSS pixels (matching the gold reference desktop viewports);
mobile captures use **390 × 844** CSS pixels.
Theme is **Dark** by default across all authenticated and unauthenticated surfaces.

| File | Viewport / Theme | Visual & Functional Target |
| --- | --- | --- |
| [01-login-desktop.png](01-login-desktop.png) | 1540 × 960 / Dark | Split-hero login matching image(8) with glowing grid and live task card |
| [02-login-mobile.png](02-login-mobile.png) | 390 × 844 / Dark | Responsive mobile dark login with brand header and password reveal |
| [03-register-mobile.png](03-register-mobile.png) | 390 × 844 / Dark | Clean dark registration view with accessible password strength meter |
| [04-verify-mobile.png](04-verify-mobile.png) | 390 × 844 / Dark | Email verification status flow |
| [05-home-desktop.png](05-home-desktop.png) | 1540 × 960 / Dark | Authentic Opus 5.5 home matching image(9): persistent sidebar, greeting, setup card, prompt-first composer, recent sessions grid |
| [06-sessions-desktop.png](06-sessions-desktop.png) | 1540 × 960 / Dark | Sessions list with compact rows, lifecycle filters, and status indicators |
| [07-session-desktop.png](07-session-desktop.png) | 1540 × 960 / Dark | Session workspace with tabs, followup composer, and details/usage side rails |
| [08-session-mobile.png](08-session-mobile.png) | 390 × 844 / Dark | Mobile session conversation, scrollable tabs, and mobile navigation |
| [09-home-mobile.png](09-home-mobile.png) | 390 × 844 / Dark | Mobile home with responsive composer chips and bottom navigation bar |
| [10-connections-mobile.png](10-connections-mobile.png) | 390 × 844 / Dark | Mobile integrations view with numbered provider cards |
| [11-connections-desktop.png](11-connections-desktop.png) | 1540 × 960 / Dark | Authentic Opus 5.5 integrations matching image(10) with 3 numbered cards |
| [12-projects-desktop.png](12-projects-desktop.png) | 1540 × 960 / Dark | Dark projects view with versioned repository spec |
| [13-settings-desktop.png](13-settings-desktop.png) | 1540 × 960 / Dark | Settings view with theme selector and API keys |
| [14-settings-dark.png](14-settings-dark.png) | 1540 × 960 / Dark | Dark settings view verifying theme persistence across reload |
| [15-home-dark.png](15-home-dark.png) | 1540 × 960 / Dark | Home workspace in persistent dark mode |
| [walkthrough.webm](walkthrough.webm) | Video (~359 KiB) | Full interaction walkthrough: login fixture → home → session composer → sessions list → session detail → connections → projects → settings |

---

## Interaction Walkthrough Video

The video file `walkthrough.webm` (~359 KiB) records the complete user flow in Chromium:
1. Desktop split-screen sign-in fixture
2. Home workspace with greeting, setup progress, prompt-first composer interactions
3. Navigation to Sessions list and opening Session detail
4. Conversation tabs and activity views
5. Mobile viewports and bottom navigation
6. Integrations / Connections provider cards
7. Projects and Settings with dark theme persistence

---

## Verification & Quality Gates

- `make lint`: All ruff checks passed; 275 files correctly formatted.
- `make console-check`: Typecheck clean, all 9 test suites (41 tests) passed in Vitest, production build succeeded.
- `make test`: All 325 pytest unit and integration tests passed (324 passed, 1 skipped).
- `make docs-check`: Documentation build and link check passed (20 slugs, 10 redirects).
- Responsive viewports verified: 320px, 390px, 768px, 899px, 900px, 1024px, 1540px with no horizontal overflow.
