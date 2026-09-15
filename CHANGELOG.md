# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this file is the
source for release notes.

## [0.1.0-alpha] - Unreleased

First public alpha. Self-hosted orchestration for cloud coding agents:
BYO Modal workspace + BYO official provider subscriptions behind one
`/v1` REST API.

### Added

- Public `/v1` API (Cursor Cloud Agents shape, Bearer `sbx_<key>`):
  agents, runs, SSE streaming with `Last-Event-ID` resume, cancel, usage,
  `/v1/models`, `/v1/me`. Contract: `docs/contracts/api-v1.yaml`.
- Durable run ledger — terminal states (`FINISHED`/`ERROR`/`CANCELLED`/
  `EXPIRED`) persist across sandbox teardown and restarts; structured
  `RunError` (`code`/`source`/`retryable`/`retry_after`).
- Multi-provider runner: `AgentAdapter` protocol + adapters for codex
  (Stable), devin / antigravity / grok (Experimental). Provider matrix and
  evidence policy: `docs/providers.md`.
- Multi-account scheduling: account registry, `account_id:"auto"` LRU pick,
  per-account slots, cooldown/failover on `auth_invalid`/`rate_limited`;
  fleets via `SBX_<PROVIDER>_ACCOUNTS`.
- Credential lifecycle: blob import (files restored `0600`), child-env
  scrub, `runner export-credentials` write-back, `/v1/accounts/{id}/verify`
  probe.
- Workspaces & artifacts: pinned base checkouts, durable artifact packages
  (manifest + `patch.diff`/`repo.bundle`, sha256-verified, secret-scan fail
  closed), cross-agent handoff, review pinning.
- Workflow recovery: persisted `metadata.workflow_id` bindings,
  `GET /v1/workflows/{id}`, scoped cleanup `DELETE /v1/workflows/{id}`;
  client-side `recover`/`close_workflow`.
- Deployment: `sbx` bootstrap CLI (init/config/deploy/doctor/smoke/
  upgrade/uninstall), named per-provider runtime images, `sbx-control`
  Modal app with reaper cron, optional Cloudflare Worker edge.
- Reference client `examples/sbx_client.py` (`httpx`-only): create, watch,
  wait, wait_many, followup, cancel, resume, recover, artifacts.

### Security

- Sandbox-only trust boundary; no Modal/platform credentials inside
  sandboxes.
- `/v1` keys stored as `sha256` only; credential material never logged;
  artifact collection fails closed on suspected secrets.

### Known limitations

- Alpha: `/v1` may change before 1.0. No hosted SaaS, no browser/noVNC
  layer, no real billing (cost is a Modal list-price estimate).
- OpenCode and Claude Code are not supported at this tag — see
  `docs/providers.md` for the honest per-provider status.
