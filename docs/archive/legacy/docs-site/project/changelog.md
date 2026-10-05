---
title: Changelog
description: Release notes and version history.
---

## [Unreleased]

### Added

- Build/deploy-time provider CLI version resolution: any `*_version` in `runtime/packages.txt` can be `latest`, which resolves once on the build host and freezes into `cli-versions.json` for reproducible rebuilds.
- Per-agent session resources: `resources.secrets` attaches Modal Secrets; `resources.mcp` resolves MCP server registry entries (Devin only).
- Validation evidence artifacts: durable, content-addressed evidence items bound to the workspace head.
- Prepared-environment snapshot cache: `environment_key` names cached sandboxes; snapshots are last-known-good with fail-closed verification.
- Automatic OAuth credential write-back: after each turn, refreshed tokens are committed to account records and the managed Secret is recreated in place (no redeploy).

## [0.1.1] - 2026-09-19

Public-alpha patch release. Control-plane and bootstrap improvements on top of `v0.1.0-alpha`. Provider pins, adapters, and frozen contracts are unchanged; `v0.1.0-alpha` evidence carries over.

### Added

- First-class git/PR workflow on `/v1`: declare git policy on create-agent, publish, review, and merge with optional gating
- Optional `output_contract`: enforce JSON Schema on agent output (strict = error, warn = keep FINISHED with verdict)
- Lifecycle timer consolidation: `SBX_TURN_MAX_SECONDS`, `SBX_SANDBOX_IDLE_TIMEOUT_S`, etc. resolve once at deploy and inject into all layers
- Workflow recovery: agents carry `metadata.workflow_id`, `GET /v1/workflows/{id}` rebuilds agent/run/artifact view for fresh processes

### Fixed

- Control-plane deploy now mounts `runtime/` into `CONTROL_IMAGE` (self-contained, no remote import failures)
- Stranded `running` sessions reconcile from on-sandbox turn evidence after restarts
- `GET /v1/artifacts` enumerates manifest keys only, not values (linear cost per artifact, not total bytes)

### Known limitations

All `v0.1.0-alpha` limitations still apply. `/v1` API may change before 1.0. Self-hosted single-workspace only. Provider coverage: codex Stable (authentication deferred), devin/antigravity/grok/opencode Experimental, claude Not supported.

## [0.1.0-alpha] - 2026-09-17

First public-alpha release. Self-hosted orchestration for cloud coding agents.

### Added

- Public `/v1` API (Cursor Cloud Agents shape, Bearer auth): agents, runs, SSE streaming with `Last-Event-ID` resume, cancel, usage, `/v1/models`, `/v1/me`
- Durable run ledger with terminal states (`FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`) and structured `RunError` (`code`, `source`, `retryable`, `retry_after`)
- Multi-provider runner with `AgentAdapter` protocol: codex (Stable), devin/antigravity/grok/opencode (Experimental — all passed real-account Modal gates)
- Multi-account scheduling: LRU picker, per-account slots, cooldown/failover on `auth_invalid`/`rate_limited`
- Credential lifecycle: blob import (restored 0600), child-env scrub, write-back, verify probe
- Workspaces & artifacts: pinned base checkouts, durable packages (manifest + patches), SHA256-verified, secret-scan fail-closed, cross-agent handoff, review pinning
- Workflow recovery: persisted `metadata.workflow_id` bindings, `GET /v1/workflows/{id}` recovery view, idempotent cleanup
