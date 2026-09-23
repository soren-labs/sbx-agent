# tests/e2e

Playwright suite for the web console. No cloud credentials, no Modal.

`web/playwright.config.ts` starts `tests/e2e/serve_console.py`: the real
control plane (`control.app.create_app()`, `SBX_BACKEND=local`) with a
test-only bootstrap key, fake CLIs for all five providers, a demo git
repository, and `web/` mounted at `/`.

```bash
make test-e2e
```

`console.spec.ts` walks the whole product surface in order: connect (bad and
good key), create an agent and stream run 1, follow up, cancel a hanging run,
reload without duplicates, a failing run's structured error, a repository
agent (git policy, review pin, publish, snapshot, artifact download),
workflow recovery and cleanup, list filters, capacity, GitHub posture, account
import/removal, API key create/revoke with scope checks, and the Chinese
light-theme UI.

Fake-CLI scenarios are chosen from prompt keywords (`hang`, `slow`, `fail`,
`auth`; otherwise success). Key steps save screenshots to `artifacts/`
(git-ignored).
