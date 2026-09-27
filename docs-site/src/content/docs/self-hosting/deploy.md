---
title: Deploy SBX
description: Fresh-clone deployment, provider/GitHub connection, verification and upgrade entry points.
---

## Fresh clone → healthy platform

A normal self-hosted installation starts with one command:

```bash
git clone https://github.com/soren-labs/sbx-browser.git
cd sbx-browser
./sbx deploy
```

`./sbx` is the zero-bootstrap launcher. It finds or installs `uv`, uses a
managed Python 3.12+ environment, and runs the CLI. On a real interactive
terminal, `deploy` also opens Modal authentication when no workspace login is
available.

`sbx deploy` is idempotent. On a fresh clone it:

- initializes the local config;
- verifies Modal authentication;
- creates the bootstrap API-key secret and durable stores;
- deploys the control plane and same-origin console;
- deploys provider runtimes that are already configured;
- verifies the live `/v1/me` endpoint;
- records the deployment URL and release/version evidence locally.

A **zero-provider deployment is healthy**. Provider and GitHub connection are
post-deploy product steps, not deployment prerequisites.

## Connect integrations after deployment

Provider:

```bash
./sbx auth login --provider devin
./sbx auth status
```

GitHub (only when you need private repositories / PR delivery):

```bash
./sbx github connect
```

Then:

```bash
./sbx doctor
./sbx open
```

The durable bootstrap admin key is stored at
`~/.local/state/sbx/bootstrap.key` (mode `0600`). `sbx open` uses a one-time
browser handoff so the long-lived key does not need to appear in a URL.

## What gets created

Names can be overridden by configuration, but a deployment typically owns:

- one Modal control-plane App;
- per-provider runtime images as providers are enabled;
- durable Dicts for tasks/runs/accounts/revisions/workspaces/workflows;
- the bootstrap/admin Secret and per-account credential Secrets;
- the web console served from the control-plane origin.

Inspect the resolved non-secret configuration with:

```bash
./sbx config
./sbx status
```

## Configuration

Most users should deploy the defaults first, then change only the settings
they understand. `~/.config/sbx/config.toml` and documented `SBX_*`
environment variables control names, concurrency, timeouts, account slots,
resources and advanced integrations.

See [Configuration](/self-hosting/configuration/) for the full operator
reference.

## Upgrade

```bash
git pull --ff-only
./sbx upgrade
./sbx doctor
```

Upgrade preserves durable Modal Dict state and rebuilds/redeploys the runtime
components. If you separately front the product with a statically bundled
edge worker, redeploy that worker after upgrading so it serves the new
Console bundle. See [Upgrade and uninstall](/self-hosting/upgrade-and-uninstall/).

## Platform vs provider health

Deployment health and provider health are intentionally separate:

- the platform can be healthy with zero providers;
- a missing/expired provider login makes that provider unavailable, not the
  whole control plane unhealthy;
- only verified accounts are eligible for scheduling.

Use `./sbx doctor` for platform checks and `./sbx auth status` for provider
account state.

## Documentation site

The docs are a separate static Astro/Starlight site:

```bash
make docs-check
make docs-deploy
```

Set `DOCS_SITE_URL=https://docs.example.com` when building the canonical
production copy so canonical links and the sitemap use the final domain. See
[Custom domains](/self-hosting/custom-domain/).

## Uninstall

```bash
./sbx uninstall
```

Use the purge flags only when you intentionally want to erase durable data or
credentials. Review [Upgrade and uninstall](/self-hosting/upgrade-and-uninstall/)
before destructive cleanup.
