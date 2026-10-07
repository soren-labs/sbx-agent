# Production release pipeline

Production is deployed from an immutable GitHub Release. Coding and review agents
do not deploy production directly.

## Release ownership

A published non-prerelease GitHub Release triggers `.github/workflows/release.yml`.
The workflow checks out the release tag and:

1. runs the repository quality gates;
2. builds and pushes `ghcr.io/soren-labs/sbx-agent` with both release and SHA tags;
3. records the immutable image digest;
4. builds the Console and Docs from the same Git SHA;
5. deploys the backend image to the production VPS;
6. verifies `/readyz`;
7. deploys the tested Console artifact directly to the `sbx-agent` Pages project;
8. deploys the tested Docs artifact directly to the `sbx-agent-docs` Pages project;
9. uploads `release-manifest.json` to the GitHub Release.

The Console stays same-origin from the browser's point of view. The Pages Function
under `console/functions/api/` proxies `/api/*` to `https://api.sbx-agent.com`.
This preserves the existing cookie and CSRF model without enabling cross-origin
session credentials.

## GitHub production environment

Create a GitHub environment named `production`. It must contain:

### Secrets

- `VPS_SSH_KEY`: private key for the dedicated deployment user.
- `CLOUDFLARE_API_TOKEN`: a scoped Cloudflare token with Pages Write for the SBX account.

### Variables

- `VPS_HOST`
- `VPS_USER`
- `VPS_PORT` (optional, defaults to `22`)
- `CLOUDFLARE_ACCOUNT_ID`

The deployment user must be able to use `sudo`, run Docker and create/update
`/opt/sbx-agent`. The workflow never copies application secrets from GitHub to the
host. On the first deployment `deploy/production/bootstrap.sh` generates the
PostgreSQL password, Vault key and runtime master key on the VPS itself. Long-lived
runtime secrets live only in `/opt/sbx-agent/.env` on the VPS. Cloudflare Pages is
deployed directly from GitHub Actions with the scoped environment secret; the VPS
does not hold Cloudflare deployment credentials.

Cloudflare has two Direct Upload Pages projects:

- `sbx-agent` serves `sbx-agent.com` (Console);
- `sbx-agent-docs` serves the documentation site. The zone must have
  `docs.sbx-agent.com` as a proxied CNAME to `sbx-agent-docs.pages.dev`, and the
  custom domain must be attached to that Pages project.

## VPS one-time bootstrap

Install Docker Engine and the Compose plugin. The first Release creates:

```text
/opt/sbx-agent/
  .env                 # generated on-host, mode 0600
```

The release workflow uploads `compose.yml` and `bootstrap.sh`, writes `release.env`
with the immutable image digest, and runs the deployment. Optional production
settings such as Resend can be added later to `.env` without moving those secrets
into GitHub.

Before changing the backend, the workflow verifies Docker/Compose and
`sbx-cloudflared.service`. Missing deployment prerequisites therefore fail the
Release before the currently running backend is stopped.

The existing `sbx-cloudflared.service` remains the public API edge and continues
forwarding `api.sbx-agent.com` to loopback port `8000`. The container publishes its
internal port `8800` only as `127.0.0.1:8000`, so no new public EC2 port is opened.

On the first container release the workflow backs up the legacy `sbx` database,
stops `sbx-hosted.service` only after the new database migration succeeds, and starts
the new container on port 8000. Both loopback readiness and the public
`https://api.sbx-agent.com/readyz` path must pass before the cutover is committed.
If either fails it restarts the previous container or legacy service. Once both are
healthy the legacy service is disabled but retained.

The pre-unification database is preserved as rollback/archive evidence but is not
automatically imported into the incompatible unified schema. This first cutover is
therefore a fresh unified control-plane state; subsequent releases reuse the durable
`postgres_data` volume normally.

`serve` already starts API workers and applies migrations, so production uses one
SBX application container rather than separate API and worker containers.

## Rollback behavior

Before an update the workflow preserves the previous `release.env`. If the new
container fails readiness, it restores the previous image and starts the previous
application version again.

Database migrations are append-only and are not automatically reversed. A release
that introduces an incompatible migration must include an explicit migration/rollback
plan before publication.
