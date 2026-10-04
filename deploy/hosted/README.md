# SBX Hosted Alpha deployment package

The build target is `https://sbx-agent.com` (React static assets on Cloudflare),
`https://api.sbx-agent.com` (long-running FastAPI on a VPS), PostgreSQL for all
control-plane records, and each user's own Modal workspace for compute. No
Cloudflare Worker proxies credentials or Session streams. Browser live output
uses Session-scoped direct sandbox SSE grants; the existing VPS relay remains a
fallback. WebSocket/terminal support is a future seam.

## Local development without credentials

```sh
npm --prefix console ci
make hosted-mock
# Open http://localhost:8791
```

This launcher strips ambient credentials, uses a private HOME/XDG/state directory
under `/tmp/sbx-hosted-mock-<uid>`, persists a generated encryption key with mode
0600, and injects SQLite only as a development store. Production requires
PostgreSQL. Reuse `--state` to reconstruct the local application. No secrets or
verification codes are printed. Registration returns a `challenge_id`; the
launcher alone exposes `/dev/email-inbox/<challenge_id>` to retrieve the
in-memory development email. The production factory has no inbox route. Use
browser Network tools to read the registration response, or register through the
API. Provider authorization buttons complete deterministic fake handshakes.

For a credential-free PostgreSQL deployment:

```sh
docker compose -f deploy/hosted/compose.mock.yaml up --build -d
# Open http://localhost:8791; only this loopback port is published.
docker compose -f deploy/hosted/compose.mock.yaml down
```

The PostgreSQL and encryption-key volumes persist across `down`/`up`; do not
remove them when testing reconstruction. `REDACTED` is the local mock database
password, never a production credential. The Docker API uses a non-root user,
explicit source copies and a credential-excluding build context. The bridge
network does not expose random sandbox listener ports to the host browser, so
Docker uses the relayed stream fallback. `make hosted-mock` and the acceptance
gate exercise direct loopback sandbox HTTP streaming.

## Automated build acceptance

```sh
# Install Chromium once into an isolated browser directory.
uv run --with playwright playwright install chromium
make lint
make test
make test-hosted-alpha
```

The Alpha gate registers/verifies/sets a password, logs out/in, connects mock
Modal/GitHub/Codex, rotates Codex refresh credentials, executes a real Git/runner
coding Session and tests, receives direct HTTP events, delivers a draft PR,
records requested fixes from a separate review Session, fixes/reviews/merges,
creates a one-time personal API key, creates/queries another Session with that
key, then starts a new OS process and verifies durable state and credentials.
It runs against SQLite and optional local PostgreSQL. Set
`SBX_TEST_POSTGRES_BIN` to the directory containing `initdb`/`pg_ctl` to enable the
private, throwaway PostgreSQL test cluster. It never connects to an existing
cluster or cloud service. Reviewer compute is released through the existing
lifecycle API before another Session starts; durable review/history remains.

## Production environment contract

`production.env.example` documents required variables. Install real values into
`/etc/sbx-hosted.env` outside the checkout with owner `sbx` and mode 0600. Preserve
`SBX_CONNECTION_ENCRYPTION_KEY` across restarts/backups: changing it makes stored
connection credentials unreadable. Use PostgreSQL's normal backup/restore
process for the database and protect the encryption key separately. Schema
migrations run transactionally at server startup. Never substitute SQLite,
Modal Dicts, or a new encryption key on a production restart.

`control.hosted_server:create_hosted_app` validates the topology and requires a
credentialed `SBX_HOSTED_ADAPTER_FACTORY=control.production_adapters:create_adapters`.
The included callable returns
exactly `email_sender`, `modal_provider`, `github_factory`, `codex_provider`, and
`compute_provider`, implementing the existing seams in `auth_email.py`,
`modal_connection.py`, `hosted_github.py`, `codex_broker.py`, and
`hosted_compute.py`. Supply provider secrets to that factory from the VPS secret
manager/environment; do not bake them into either image or static assets.
Modal and AI refresh credentials remain encrypted on the VPS. Only short-lived,
access-only Codex leases enter sandboxes. Browser keys authorize the owning
user's connections. Personal keys support the `agents` scope and optional expiry;
they cannot mint administrator credentials or manage other keys.

The production adapters use Resend, explicit user Modal SDK credentials,
repository-scoped GitHub App installation tokens, and official native Codex
app-server authorization/refresh. GitHub owner association is approved by the
trusted operator; an arbitrary browser-supplied installation ID cannot claim it.
Codex supports native device consent or a trusted encrypted migration of the
previously authorized dedicated test-user connection. See
[GitHub integration](../../docs/hosted-github.md) and
[Codex integration](../../docs/hosted-codex.md) for the authorization boundaries.
The production factory refuses mock mode, missing adapters, or a missing native
Codex executable. Hosted operator Basic
auth is disabled by default; a migration operator can explicitly enable it with
separate strong credentials. End-user browser login uses only email/password.

## Prepared VPS and Cloudflare Pages rollout

The production VPS uses local PostgreSQL peer authentication for OS user `sbx`,
a loopback API on `127.0.0.1:8000`, and the existing `sbx-cloudflared` connector.
The tunnel targets `http://localhost:8000`; it does not take the public port 443
owned by the existing sing-box workload. Preserve both existing services.

The explicit operator command below uploads only source plus selected service
secrets over SSH stdin. It preserves the stable encryption key, existing database,
Git histories and tunnel. It never reads development-agent Codex auth, uploads a
raw native cache, or exports ambient cloud/test-workspace credentials. The root
installer uses Python 3.12/uv and installs the pinned official ARM64 Codex CLI
0.159.2. Both initial deployment and redeployment restart the systemd service.
Rollout requires a clean checkout, packages only committed Git blobs, and
verifies the uploaded bytes against the pinned commit before writing the
release manifest. Releases use separate directories with atomic activation.

```sh
# Source the protected operator env quietly; never enable shell tracing.
set -a
source /home/zheng/.config/sbx/real-integration.env
uv run python -m deploy.hosted.rollout
bash deploy/hosted/build_frontend.sh
# From a directory without a conflicting Worker config:
npm exec --yes --package=wrangler -- wrangler pages deploy /absolute/path/to/console/dist --project-name sbx-agent --branch main --commit-dirty=false
```

The protected environment/private key live outside `/opt/sbx-browser`, with mode
0600 and owner `sbx`. Native refresh caches exist only under the service's private
`/run/sbx-hosted` tmpfs and are removed after each operation. Systemd restricts
writes to that runtime directory and `/var/lib/sbx-hosted`. PostgreSQL migrations
run at startup. One worker retains the current scheduler/watcher model and
reconciles owner-scoped runtimes and idle checkpoints every 30 seconds. Idle
compute releases account capacity; durable queued prompts survive reaping.

Cloudflare Pages serves `sbx-agent.com`; its custom domain must be active and point
to `sbx-agent.pages.dev`. The existing tunnel serves `api.sbx-agent.com`. Exact-origin
CORS permits credentialed requests only from `https://sbx-agent.com`; JSON/origin
CSRF checks remain active. The browser cookie is host-only, HttpOnly, Secure,
SameSite=Lax. User runtime images receive this exact origin for direct SSE CORS.
No provider credentials are frontend build inputs. `/hosted/health` verifies the
database; HTTPS on both hosts must validate normal certificates.

The opt-in production gate performs real browser OTP/password login and Modal
connection, trusted App/native bootstrap, native coding and direct live SSE,
App PR delivery, independent native review, intended test-repository merge,
personal-key API Session, native rotation and control-plane restart persistence:

```sh
uv run --with playwright python -m deploy.hosted.gates.production
```

Its resumable private state is mode 0600 under `/home/zheng/.config/sbx/` and is
never an artifact. Once imported credentials rotate on the VPS, its encrypted
PostgreSQL connection is canonical; do not reimport the stale dedicated local
refresh grant. Implementation PRs remain unmerged; product merge acceptance is
restricted to a disposable branch in `soren-labs/sbx-e2e-test`. Gate output includes
only bounded status/evidence. See the stage review records for external limits.

## Programmatic use

Create a personal key under Settings → API Keys. Save the creation response
once; list/reload never returns plaintext. Send `Authorization: Bearer <key>` to
`https://api.sbx-agent.com`. The existing endpoints remain available:

| Operation | Endpoint |
| --- | --- |
| Create/list/get Session | `/v2/sessions`, `/v2/sessions/{id}` |
| Follow-up/cancel/retry | `/v2/sessions/{id}/messages`, `/cancel`, `/retry` |
| Event stream/history | `/v2/sessions/{id}/events`, `/history` |
| Changes/diff/delivery | `/v2/sessions/{id}/changes`, `/changes/diff`, `/deliver` |
| Start/read independent review | `/hosted/sessions/{id}/review-sessions`, `/hosted/review-sessions/{review_id}` |
| Durable review records/merge | `/v1/tasks/{id}/reviews`, `/v1/tasks/{id}/merge` |
| Release retained compute | `DELETE /v1/agents/{agent_id}` |

Use existing idempotency keys where supported. Owner identity stays stable
across personal-key rotation and browser/API access. Revocation/expiry returns
401 on the next request; a personal key has no key-management authority.
