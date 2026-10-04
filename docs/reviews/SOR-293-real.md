# SOR-293 production deployment and real workflow

Stack base: `hosted-alpha/sor-292-real-codex`, `29f1bae` (PR #160).

The latest integration source runs on the prepared ARM64 Ubuntu VPS, with durable
local PostgreSQL peer authentication as service user `sbx`. Cloudflare Pages serves
`https://sbx-agent.com`; the existing Cloudflare Tunnel serves
`https://api.sbx-agent.com` to loopback port 8000. Both have valid HTTPS certificates.
The existing sing-box process and its public port 443 are preserved and active.
No implementation PR was merged.

The production factory wires real Resend, credential-bound user Modal operations,
owner-approved GitHub App tokens, and the official native Codex broker. Deployment
found and fixed concrete issues: the VPS virtualenv runner path does not exist
inside user compute, and an npm installation prefix can put Codex outside the
configured control-plane executable path. Compute now invokes the image's Python;
the installer pins `/usr/local/bin/codex` and startup validates executable access.
The hosted review panel also lacked the non-hosted panel's external PR link; it
now exposes the real GitHub delivery URL. Redeploy always restarts the service.
One worker retains the existing watcher model.

Service secrets/App PEM remain mode 0600 outside the checkout. No development-agent
credentials or ambient AWS/Cloudflare/Modal test credentials are deployed. The
preauthorized dedicated test-user connection was validated and migrated as an
AES-GCM encrypted envelope over SSH stdin, then stored under the production vault
in PostgreSQL. The raw native auth file was never copied. Native temporary refresh
caches live in protected service tmpfs and are removed after operations. After real
production rotation, the VPS encrypted connection is canonical; the local dedicated
cache must not be reimported as the latest grant. The development-agent login remains
independent and healthy.

Real production acceptance uses a fresh browser user and the actual receiving
Resend inbox: email OTP, password setup, later password login, Secure/HttpOnly/
SameSite=Lax host-only cookie, manual real Modal connection/Ready, approved real
GitHub repository and native Codex connection. A real coding Session creates two
files, commits them, and runs its unittest. Direct Modal HTTPS/SSE returns the
approved browser CORS origin, carries native activity while work is running, and
reports a unittest command exit 0. Captured native events pass actual credential
leak checks. The deployed browser also renders durable native command activity,
changed files, the real GitHub PR URL and merged delivery. A browser-created
personal key is hidden on reload and successfully queries the durable API Session.


The App creates test PR #9 in `soren-labs/sbx-e2e-test`. A separate real native
review Session reviews its exact head, returns approve, and the existing merge gate
merges it into its disposable `sbx-production/base-*` branch. The repository's main
branch and all implementation PRs remain untouched. The request-changes/fix path
was exercised in the SOR-291 real GitHub gate; this production reviewer approved.

A personal API key creates and queries another real native Session. Official native
refresh crosses a forced broker expiry boundary and rotates the stored credential
version. After restarting the API, all encrypted integrations, delivered Sessions,
review records and the personal key persist. The API key starts another native turn
in the retained user-owned sandbox after rotation/restart, with a successful unittest
command in its durable activity. Production journal checks cover actual Codex,
Modal, email and App secrets, plus token-shaped output. Exact-origin CORS accepts
the production frontend and rejects an unapproved origin. Redeploy retains the
database, vault key and integrations. Every VPS source artifact is verified against
its per-file SHA256 manifest; the release record includes the implementation commit
and whether the source tree is dirty. The static build records its source commit
and asset hashes separately.


Validation: `make lint` PASS; `make test`: 3,439 passed, 14 skipped, two upstream
deprecation warnings. Console tests: 122 passed. The optional skips remain existing
local PostgreSQL/image/browser checks; real production acceptance is a separate
opt-in gate. An initial full run found a test import binding error (corrected) and
an existing shutdown timing failure; focused checks and the final full run pass.

External limits remain explicit: GitHub account/repository ownership uses trusted
operator approval, not an unregistered OAuth client. Native Codex device consent
uses the official CLI mechanism, not a separately registered hosted SIWC client.
Upstream JWT lifetime is controlled by OpenAI; SBX's lease deadline cannot shorten
it. Actual account-wide revocation is not performed: SOR-292 exercises invalid-grant
and 401 faults with real refresh instead. Production review passed without a fix.
The operator's stale frontend DNS proxy was bypassed with public DNS mapping and
normal certificate validation; TLS validation was never disabled.

Frozen contracts and PR histories #149–156 remain unchanged. PRs #157–160 remain
open and stacked. This stage supplies the final stacked deployment PR.
