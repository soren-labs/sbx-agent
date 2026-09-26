"""Sorenforge GitHub integration broker (SOR-220).

A small, trusted, centrally hosted service that lets every self-hosted SBX
deployment connect GitHub in **one click**: the browser lands directly on the
pre-registered *public* Sorenforge GitHub App's installation page — no App
creation, no PAT, no PEM, no env vars, no redeploy.

Trust boundary — the broker is the only component that ever holds the shared
GitHub App's private key. It is deliberately narrow:

- create a short-lived, HMAC-signed, single-use ``state`` binding the install
  flow to one specific deployment's callback URL;
- receive GitHub's post-installation ``setup`` callback and associate
  ``installation_id`` / account / repository scope with that deployment;
- hand the deployment a one-time claim code it exchanges for the installation
  metadata plus a per-installation broker credential;
- mint/broker short-lived GitHub App installation access tokens for the
  bound deployment, least-privilege scoped to the authorized repositories;
- never expose the App private key — not in responses, logs, or any data
  shipped back to a deployment.

The App private key arrives via ``SBX_BROKER_APP_PRIVATE_KEY`` (env/Secret)
and lives only inside :class:`broker.service.BrokerConfig` for the lifetime
of the process. Self-hosted SBX deployments store only non-sensitive
metadata (installation id, account, repository coverage) plus the
broker-issued per-installation credential — a revocable bearer that cannot
sign App JWTs and is scoped to exactly one installation.

A deployment that refuses any hosted component can still run fully
self-hosted: the operator-supplied App env config (SOR-177) and the
per-deployment App Manifest registration flow remain available under
Advanced — the broker is the default, never the only path.
"""
