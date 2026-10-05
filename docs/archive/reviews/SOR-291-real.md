# SOR-291 real GitHub App integration

Stack base: `hosted-alpha/sor-290-real-modal`, `cebbb0c` (PR #158).

Production uses the existing SBX Agent App private key and short-lived
installation tokens. Neither device flow nor an OAuth client secret is required.
The operator binds an explicitly approved installation/repository set to an
existing authenticated SBX user through a control-plane-only CLI. Browser
callbacks cannot claim an arbitrary App installation. The installation link is
GitHub's supported App installation URL; GitHub is an integration, not SBX login.

Bindings and installation metadata are durable owner-scoped PostgreSQL records.
Live repository selection, suspension and uninstall are observed before token
minting and repository listing. Tokens are narrowed to approved repositories.
The App private key remains on the control plane. Provider response bodies never
escape the REST adapter. App JWTs reserve clock-skew margin inside a ten-minute
window. Production payload/ref helpers now use real Git rather than mock repos.

The opt-in gate uses `soren-labs/sbx-e2e-test` only, a disposable base branch,
real App tokens, real GitHub REST/Git operations and real Modal sandboxes.
AI output is deterministic in this stage; SOR-292/293 cover real Codex inference.
Real gate PASS: real Modal coding Session with clone/fetch, branch/commit and
tests; App-token push; real draft PR; independent review requesting changes;
fix and ready PR; another independent approval; stale-review detection;
intended merge gate into the disposable base branch. Dedicated test-repo PR #8
was merged through SBX. Actual installation-token revocation returned 204 and
subsequent use returned 401. Durable owner disconnect removed effective access.
All gate sandboxes and the temporary PostgreSQL container were cleaned up.

`make lint` PASS; `make test`: 3,428 passed, 14 skipped. Focused checks: 10
passed. Console typecheck and tests: 122 passed.

Disconnect removes only the owner's binding, avoiding a destructive uninstall
of an installation shared by other SBX users. Actual installation-token
revocation is safely testable without uninstalling SBX Agent. Upstream uninstall,
repository removal and suspension also have credential-free regression coverage.

Alpha limitation: self-service GitHub user OAuth is not configured. The trusted
operator approval step supplies the ownership association instead; it must not
be replaced with trusting a browser-provided installation_id or first-user wins.
Frozen contracts and old PR histories are unchanged. Deployment is SOR-293.
