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
  (Stable), devin / antigravity / grok / opencode (Experimental — all
  four passed real-account Modal gates on the RC plane; codex's RC lane
  is credential-deferred on a stale ChatGPT token, external). Provider
  matrix and evidence policy: `docs/providers.md`.
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
- Account ids are validated fail-closed (`[A-Za-z0-9._-]`, alnum first,
  ≤128 chars) before they reach `FileAccountStore` paths, `modal.Dict`
  keys, or `sbx-acct-<id>` Secret names — traversal/absolute/encoded ids
  are refused across the legacy `control.accounts` CLI, registry,
  scheduler, onboarding, and `/v1/accounts/{id}` routes (404/409, no
  store I/O). Stored records whose body id is unsafe or foreign to their
  store key decode as disabled corrupt records (SOR-105).

### Fixed

- Antigravity release pin moved `agy_version` 1.2.2 → **1.2.3** after
  re-validating the headless contract against the real 1.2.3 binary
  (`--version` output, stream-json init/step/result shapes,
  `--conversation` resume + stale-id semantics, credential path) — no
  adapter or gate changes required (SOR-106).
- `sbx uninstall` no longer reports a clean teardown when Modal is
  unreachable: `ModalPlane` raises `BootstrapError` on auth/network
  failures instead of flattening them to "empty"/"absent" (clean-room
  acceptance SOR-100).
- `sbx uninstall` can actually stop the app: `modal app stop` now runs
  with `--yes` instead of dying on the interactive `[y/N]` prompt
  (SOR-100).
- OpenCode seeded default models named ids that do not exist on the
  account's real auth channels (`anthropic/claude-sonnet-4.5` /
  `openai/gpt-5.3-codex`); corrected to `openai/gpt-5.6-luna` +
  `opencode/claude-sonnet-4-5` (`SBX_OPENCODE_MODELS` still overrides) —
  found by the Release 0.1 core gate.
- A `running` record stranded by a control-plane cutover held its account
  slot forever; the reaper now finalizes it `lost` past the runner's own
  `--max-seconds` bound plus a 300 s grace, and a dead-sandbox follow-up
  maps to `409 session_not_runnable` instead of a bare 500 — found by the
  Release 0.1 grok gate.

### Known limitations

- Alpha: `/v1` may change before 1.0. No hosted SaaS, no browser/noVNC
  layer, no real billing (cost is a Modal list-price estimate).
- Codex's Release 0.1 RC gate lane is **CREDENTIAL_DEFERRED** — the
  workspace ChatGPT token is stale and noninteractive refresh fails;
  restoring the lane needs an interactive `codex login` (external, not a
  product defect; the earlier real-Modal suite evidence stands). Claude
  Code is not supported (experimental adapter seam merged but
  unregistered) — see `docs/providers.md` for the honest per-provider
  status.
