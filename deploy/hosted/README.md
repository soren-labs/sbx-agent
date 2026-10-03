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
credentialed `SBX_HOSTED_ADAPTER_FACTORY=module:callable`. That callable returns
exactly `email_sender`, `modal_provider`, `github_factory`, `codex_provider`, and
`compute_provider`, implementing the existing seams in `auth_email.py`,
`modal_connection.py`, `hosted_github.py`, `codex_broker.py`, and
`hosted_compute.py`. Supply provider secrets to that factory from the VPS secret
manager/environment; do not bake them into either image or static assets.
Modal and AI refresh credentials remain encrypted on the VPS. Only short-lived,
access-only Codex leases enter sandboxes. Browser keys authorize the owning
user's connections. Personal keys support the `agents` scope and optional expiry;
they cannot mint administrator credentials or manage other keys.

Real provider adapters/credentials are intentionally required before production
acceptance. The build package's fake providers prove state and API contracts,
not real Modal ingress, GitHub permissions, or Codex subscription behavior. The
production factory refuses mock mode or missing adapters. Hosted operator Basic
auth is disabled by default; a migration operator can explicitly enable it with
separate strong credentials. End-user browser login uses only email/password.

## VPS and static asset rollout preparation

1. Install Python 3.12+, Git, PostgreSQL and `uv` on the VPS. Create service user
   `sbx`, check out the reviewed build to `/opt/sbx-browser`, and run
   `uv sync --frozen --no-dev`. Alternatively build the Dockerfile's `api` target
   and supply the same external environment contract.
2. Install credentialed adapters and the protected env file. Install
   `sbx-hosted.service`; it runs one worker because the runner watcher/scheduler
   lifecycle is process-local. Bind only to loopback behind the supplied
   `Caddyfile`. Trust forwarded headers only from that local proxy. Access logs
   are disabled, and neither cookie nor bearer headers belong in proxy logs.
3. Run `build_frontend.sh`. It sets `VITE_HOSTED=1` and
   `VITE_API_BASE=https://api.sbx-agent.com`; no provider credentials are frontend
   build inputs. `wrangler.toml` is an assets-only Cloudflare target, with SPA
   fallback and no account/token embedded. Review the generated assets, then
   separately deploy using the operator's authenticated Cloudflare environment.
4. Provision DNS/TLS for the two hosts. Do not use credential forwarding through
   an edge worker. Exact-origin CORS allows credentialed requests only from
   `https://sbx-agent.com`; the API still enforces JSON/origin CSRF checks. The
   browser cookie remains host-only, HttpOnly, Secure and SameSite=Lax; the two
   production hosts are same-site. Runtime image launchers receive that explicit
   browser origin for direct SSE CORS. `/hosted/health` probes the database.
5. Validate real email delivery, Modal workspace/image/ingress, user-bound GitHub
   installation permissions and repository-token expiry, Codex OAuth/refresh/
   revocation and three concurrent Sessions, direct-stream reconnect/expiry,
   backup restoration and the full coding/PR/review/merge gate. Real production
   deployment, DNS changes, independent review and merging these build PRs are
   separate later work.

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
