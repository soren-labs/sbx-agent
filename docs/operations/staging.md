# Staging environment

Staging is a persistent, production-shaped backend used to accept a candidate
before it is released. It is isolated from production: its own host, database,
Vault key, runtime master key and test accounts. Nothing in this document reads
from, or writes to, production.

## Tiers

| Tier | Runs | Does not run |
| --- | --- | --- |
| GitHub-hosted CI | lint, unit/integration tests, Console checks, image build, mocked browser tests | real provider logins |
| Staging VPS (2 vCPU / 2 GiB) | control plane (API + in-process workers), PostgreSQL, the tunnel edge | browsers, test suites, image builds |
| Production | a released, accepted revision | anything untested |

The browser client for staging acceptance runs on GitHub Actions and talks to
staging over its public URL. Chromium is never installed on the staging host.

## Host layout

```text
/opt/sbx-staging/
  .env                  # generated on-host by bootstrap.sh, mode 0600
  compose.yml           # deploy/staging/compose.yml
  deploy.sh             # deploy/staging/deploy.sh
  release.env           # image digest + git SHA of the current rollout
  release.env.last-good # last rollout that passed readiness
  backups/              # pg_dump taken before each rollout (last 5 kept)
```

The Compose project is `sbx-staging`; the durable volumes are
`sbx-staging_postgres_data` and `sbx-staging_sbx_data`. The control plane
listens only on `127.0.0.1:8000`.

Memory budget on the 2 GiB host: the application container is capped at 900 MiB
and PostgreSQL at 512 MiB (`shared_buffers=128MB`, `max_connections=40`), with
`SBX_WORKER_THREADS=2`. `bootstrap.sh` adds a 1 GiB swap file with
`vm.swappiness=10` as an out-of-memory safety margin. `SBX_EXECUTORS=modal`:
agent execution happens on Modal, never on the staging host.

## One-time bootstrap

Run from an operator machine that has SSH access to the staging host:

```bash
scp deploy/staging/bootstrap.sh deploy/staging/compose.yml deploy/staging/deploy.sh sbx-staging:/tmp/
ssh sbx-staging 'sudo sh /tmp/bootstrap.sh \
  && sudo install -m 0644 /tmp/compose.yml /opt/sbx-staging/compose.yml \
  && sudo install -m 0755 /tmp/deploy.sh /opt/sbx-staging/deploy.sh'
```

`bootstrap.sh` is idempotent. It installs Docker Engine and the Compose plugin
from the distribution, enables swap, and generates `/opt/sbx-staging/.env` with
new random secrets. It refuses to run on a host that has `/opt/sbx-agent`
(the production layout). Secrets are generated on the host and are not copied
to GitHub or to any other machine.

## Candidate image

`.github/workflows/staging-image.yml` builds the backend image for every commit
on `main` and pushes it as `ghcr.io/soren-labs/sbx-agent:staging-<git sha>`.
The image digest is printed in the workflow run summary. It can also be read
from the registry without any Docker plugin:

```bash
sha=<git sha>
token=$(curl -fsS "https://ghcr.io/token?scope=repository:soren-labs/sbx-agent:pull" | jq -r .token)
curl -fsSI -H "Authorization: Bearer $token" \
  -H "Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json" \
  "https://ghcr.io/v2/soren-labs/sbx-agent/manifests/staging-$sha" | grep -i '^docker-content-digest'
```

These tags are separate from the `v*` and `sha-*` tags written by the
production release workflow.

## Rollout

```bash
ssh sbx-staging 'sudo /opt/sbx-staging/deploy.sh \
  ghcr.io/soren-labs/sbx-agent@sha256:<digest> <git sha>'
```

`deploy.sh` accepts only a digest-pinned image, takes a host lock so rollouts
cannot overlap, dumps the database, pulls the image, applies migrations, and
recreates the application container. The rollout is accepted only when
`/readyz` passes and the running container carries the expected image
reference and `org.opencontainers.image.revision` label. Otherwise the last
known-good release is restored and the script exits non-zero.

Database migrations are append-only and are not reversed by a rollback; the
pre-rollout dump in `backups/` is the recovery point.

## Checks

```bash
ssh sbx-staging 'curl -fsS http://127.0.0.1:8000/readyz'
ssh sbx-staging 'cat /opt/sbx-staging/release.env.last-good'
ssh sbx-staging 'sudo docker compose -p sbx-staging ps'
ssh sbx-staging 'sudo docker compose -p sbx-staging logs --tail=100 sbx'
```

Both containers use `restart: unless-stopped` and Docker is enabled at boot, so
the stack returns after a host reboot with the same database volume.

## Test users and acceptance

Staging has two stable test users, each with its own workspace and Connections.
Their Connections (Modal, BYOK inference, and GitHub for user A) are stored once in
the staging Vault. A client that runs acceptance therefore needs only the two
users' sign-in credentials; provider tokens never go to GitHub.

One-time setup from an operator machine (idempotent; safe to re-run):

```bash
export SBX_STAGING_URL=https://api-staging.sbx-agent.com
export SBX_STAGING_USER_A_EMAIL=... SBX_STAGING_USER_A_PASSWORD=...
export SBX_STAGING_USER_B_EMAIL=... SBX_STAGING_USER_B_PASSWORD=...
export SBX_TEST_MODAL_TOKEN_ID=... SBX_TEST_MODAL_TOKEN_SECRET=...
export SBX_TEST_INFERENCE_API_KEY=...
export SBX_TEST_GITHUB_TOKEN=...      # token limited to the E2E test repository
uv run python -m tests.e2e_staging.bootstrap
```

Without Resend, verification mail stays on the staging host; the bootstrap reads
the test user's own verification link through `SBX_STAGING_MAIL_COMMAND` (default:
an SSH command that prints the stored mail). Exit code `2` means a provider
credential was not supplied and is still pending.

Acceptance, from any machine that can reach staging:

```bash
uv run python -m tests.e2e_staging.acceptance
```

It checks readiness, that the two users cannot read each other's Sessions, Turns
or Connections, stored Connection health, one real Turn on Modal, lease release,
and that Sessions from earlier runs are still readable. Every Session it creates
is released and closed, also when a gate fails, and a lease that is still live at
the end fails the run. It also requires the backend (`/healthz`) and the Console
(`SBX_STAGING_CONSOLE_URL`, `build-manifest.json`) to report the same commit, and
that commit to equal `SBX_STAGING_EXPECT_SHA` when it is set; this check cannot
be turned off for staging. Both scripts refuse to run against production hostnames.

Subscription Machine Slots (Codex, Grok, Antigravity) need a one-time official
sign-in by the account owner inside the Slot. Until a Slot is signed in, the run
lists it as `pending`; it is never reported as passed.
`SBX_STAGING_REQUIRE_SLOTS=codex,...` turns a missing Slot into a failure.

`SBX_STAGING_EXECUTOR=local` runs the same client against the fake-provider
stack from `tests/e2e_browser/server.py`. That is a rehearsal of the client and
is labelled as such in its output; it is not staging acceptance.

## Owner account

The staging owner may keep one personal, long-lived account on staging for manual
use. It is separate from the two automated test users:

- its credentials are never placed in GitHub (not in the `staging` environment,
  not in any workflow) and are not used by any automated test;
- the acceptance client only touches Sessions it created itself (title prefix
  `staging-acceptance`) in the two test users' workspaces;
- provider sign-ins and tokens are entered by the owner in the staging Console
  and stay in the staging Vault or, for subscriptions, in the Machine Slot's
  Modal Volume. Nobody copies them elsewhere.

Setup checklist for the owner, all at `https://staging.sbx-agent.com`:

1. Register, then verify the email (see below).
2. Connections: a Modal workspace used only for staging; BYOK inference
   (`inference_api`) and/or OpenCode Zen; a GitHub token limited to the E2E test
   repository.
3. Machine Slots, each with the provider's official sign-in inside the Slot.
   Implemented subscription providers: **Codex, Grok, Antigravity**. There is no
   Claude subscription Slot; Claude models are reachable only through a BYOK
   inference Connection.

Email verification: staging has `SBX_RESEND_API_KEY` and `SBX_MAIL_FROM` unset,
so verification mail is written to the outbox on the staging host and is not
delivered. Two supported paths:

- **One-time link (default).** After registering, an operator with host access
  runs

  ```bash
  ssh sbx-staging 'sudo bash -s <email>' < deploy/staging/verification-link.sh
  ```

  which prints only the newest verification link for that address. The owner
  opens it once; the token is single-use.
- **Scoped Resend.** Add `SBX_RESEND_API_KEY` (a sending-only key restricted to
  a staging sender domain) and `SBX_MAIL_FROM` to `/opt/sbx-staging/.env` on the
  host and re-run the rollout. The key must not be the production key and is
  never stored in GitHub.

## Public edge

- `https://api-staging.sbx-agent.com` — a staging-only Cloudflare Tunnel
  (`cloudflared-sbx-staging.service` on the staging host) forwarding to
  `http://127.0.0.1:8000`.
- `https://staging.sbx-agent.com` — the Cloudflare Pages project
  `sbx-agent-staging`, whose function environment sets
  `SBX_API_ORIGIN=https://api-staging.sbx-agent.com`, so `/api/*` on the staging
  Console reaches only the staging API.

`SBX_PUBLIC_URL` and `SBX_ALLOWED_ORIGINS` in `/opt/sbx-staging/.env` are the
staging Console origin. Production hostnames, the production tunnel and the
production Pages project `sbx-agent` are separate and are never targeted by
staging tooling. The tunnel token lives only on the staging host.

External clients use these HTTPS URLs. The operator SSH key for the host does
not permit port forwarding, and nothing here depends on it.

## Revision check

A candidate is one commit. Before acceptance, both halves must report it:

```bash
curl -fsS https://api-staging.sbx-agent.com/healthz          # .git_sha  (backend image)
curl -fsS https://staging.sbx-agent.com/build-manifest.json  # .git_sha  (Console build)
```

The `Staging acceptance` workflow and the acceptance client fail when the two
differ from each other or from the commit being accepted. There is no option to
skip this.

## Acceptance workflow

`.github/workflows/staging-acceptance.yml` is started manually on `main`
(`workflow_dispatch`). It runs in the `staging` GitHub environment, which holds
only the two test users' sign-in credentials, and in the `staging` concurrency
group so that runs never overlap. It executes the API acceptance above and the
browser tests in `console/e2e-staging` (headless Chromium on the runner, signed
in as test user A on the real staging Console), then uploads the evidence JSON
and, on failure, the Playwright trace. It is never triggered by pull requests,
so code from forks cannot read the staging credentials or reach staging.
