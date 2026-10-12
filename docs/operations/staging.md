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
The image digest is printed in the workflow run summary and can be resolved
from the tag:

```bash
docker buildx imagetools inspect ghcr.io/soren-labs/sbx-agent:staging-<git sha> \
  --format '{{.Manifest.Digest}}'
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
ssh -N -L 8000:127.0.0.1:8000 sbx-staging   # private API access from the operator machine
```

Both containers use `restart: unless-stopped` and Docker is enabled at boot, so
the stack returns after a host reboot with the same database volume.

## Public edge (pending)

Staging has no public hostname yet. The intended shape mirrors production:

- a staging-only Cloudflare Tunnel on the staging host forwarding
  `api-staging.sbx-agent.com` to `http://127.0.0.1:8000`;
- a separate Cloudflare Pages project for the staging Console at
  `staging.sbx-agent.com`, with `SBX_API_ORIGIN` pointing at the staging API.

Creating the tunnel, the DNS records and the Pages project requires Cloudflare
account access and is an explicit operator step. Production hostnames, the
production tunnel and the production Pages projects are not reused or changed.
`SBX_PUBLIC_URL` and `SBX_ALLOWED_ORIGINS` in `/opt/sbx-staging/.env` must match
the staging Console origin once it exists.
