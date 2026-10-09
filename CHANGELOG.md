# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this file is the
source for release notes.

## [Unreleased]

### Added
- Generic bring-your-own-key inference Connections (`inference_api`): an API key, a default
  model and one base URL per wire protocol (`openai_chat`, `openai_responses`,
  `anthropic_messages`). The Harness is decoupled from the model provider; DeepSeek is the
  Console preset and any compatible provider works. Outbound base URLs must be public HTTPS.
- Five official CLI Harnesses, each verified with real tool-using Turns and native session
  resume on DeepSeek, locally and in Modal sandboxes: OpenCode, Codex, Claude Code, Grok Build
  and Command Code. Manifests declare `inference_protocols`; pinned CLI versions are baked into
  the executor image.
- Console: inference key form with per-protocol base URLs and validation error states,
  Harness and model selection scoped to compatible Connections, real per-Turn token usage.
- SDK/CLI: `connections.add_inference()`, `harnesses()`, `sbx connections add inference_api
  --endpoint … --model …`, `sbx sessions create --harness …`, `sbx harnesses`.

- Console Session workbench: conversation and workspace panel side by side, tool calls grouped
  into expandable "Working"/"Worked" blocks with readable input and bounded output, explicit
  queued/starting/working/completed/failed Turn states with Stop and Retry, a docked follow-up
  composer, scroll position preserved while reading history, and a worded Activity timeline.
- Console: copy-once API keys with confirmed revoke; Projects in the navigation; translated shell.

### Changed
- OpenCode Zen keys and uploaded Codex `auth.json` credentials are retired from new flows.
  Stored Connections are preserved (migration `0004` only widens the kind constraint), listed
  as retired, and keep serving the Sessions already pinned to them.
- Native CLI state in checkpoints is stored by home-relative path so every Harness resumes
  after a restore; older checkpoints still restore.

### Fixed
- The Session event stream now detects silently dropped connections (heartbeat watchdog and the
  browser's offline event) and shows a reconnecting state instead of freezing.
- Model ids and base URLs of inference Connections were registered as secrets and redacted out
  of error messages; only the API key is.
- Console workspace ignored the light theme (always dark, with unreadable light inputs);
  composer popovers opened under the top bar; the Connections page, model picker, repository
  picker and Session usage panel showed placeholder data instead of server state.

## [0.1.2] - 2026-10-08

- Restored the polished Opus 5.5 Console experience on top of the unified API/state architecture, including the split-screen auth flow, session-rich dark workspace shell, prompt-first Home, provider-card Integrations, responsive mobile views, real-browser screenshots, and interaction video evidence.
- Renamed the project from `sbx-browser` to `sbx-agent`; the CLI command, Python import package, `SBX_*` environment variables, and `sbx-runtime` names remain stable.
- Added the production container/release pipeline: GHCR image publishing, VPS Compose deployment, Cloudflare Pages deployment for Console and Docs, release manifests, readiness checks, and image rollback.

### Changed — unified architecture (RFC 167, breaking)

- One durable domain: Projects/ProjectVersions, Sessions with logical Worktrees, Turns,
  Executions, immutable ChangeSets, exact-subject Deliveries and child-Session Delegations,
  persisted only in PostgreSQL through typed repositories, a journal and claimed/fenced Jobs.
- `sbx-runtime` daemon with official CLI Harnesses (OpenCode, Codex) on a local or Modal
  Executor; credentials are delivered as lease-scoped grants from encrypted CredentialVersions.
- One HTTP surface, `/api` (generated OpenAPI in `docs/specs/unified/openapi.yaml`), email/password
  product auth and API keys, manual Connections (Modal, GitHub token, OpenCode Zen, Codex).
- Rewritten Console (single typed client/live store) and a new SDK/CLI (`SBXClient`, `sbx`).

### Removed

- Basic `/api/*`, public `/v1`, `/v2` and hosted routes; the legacy web dashboard (`web/`),
  root `broker/`, hosted deploy tooling, the shell runner and its provider adapters (Devin,
  Antigravity, Grok and Claude are listed as disabled Harness manifests until their gates
  pass), Modal Dict/file business stores, the GitHub App requirement and the Task/Agent/Run SDK. Retired contracts and design notes moved to `docs/archive/`.

## Pre-unification unreleased changes (superseded by RFC 167)

### Added

- Redesigned web console (`web/`) on the public `/v1` API with Bearer keys:
  agents list with live counts and filters; a sectioned **New agent** form
  (provider/model/effort from `GET /v1/models`, repository + handoff, git
  policy, output contract, workflow binding, compute/resources) with a live
  JSON/cURL/Python request preview and an `Idempotency-Key`; a streaming
  conversation per agent (canonical events, structured run errors, output
  contract verdicts, follow-ups, cancel, close); a workspace tab with the
  base → head → reviewed → published → merged pipeline and review pin /
  publish / merge / handoff actions; artifacts (snapshot, manifest, member
  downloads, hand off to a new agent); workflow recovery and scoped cleanup;
  capacity; admin pages for accounts, API keys and the GitHub App; English
  and Simplified Chinese; light/dark themes. The console no longer uses the
  legacy `/api/*` surface.
- Documentation website in `docs-site/` (Astro Starlight): getting started,
  guides, operations, providers and reference pages, a console guide with
  screenshots, partial Simplified Chinese, and a REST reference generated from
  `docs/contracts/api-v1.yaml`. `make docs-dev` / `make docs-build`.
- `make console-dev` (`tests/e2e/serve_console.py`): the console against a
  real, cloud-free control plane (local backend, fake CLIs for all five
  providers, demo git repo). `make test-e2e` now runs a Playwright suite over
  the whole console against it.
- Build/deploy-time provider CLI version resolution (SOR-175): any
  `*_version` in `runtime/packages.txt` — or its `SBX_<PROVIDER>_VERSION`
  env override — may be `latest`. `runtime/versions.py` resolves it once
  on the build host (npm `latest` dist-tag for codex/opencode, the
  promoted `{devin_base_url}/current/manifest.json` + per-platform sha256
  checksums for devin, the host binary's `--version` for agy/grok) and
  freezes the concrete set into the deployment's `cli-versions.json` lock
  — recorded in `deploy.json` (`cli_versions`, `versions_lock`), replayed
  verbatim by `sbx deploy --versions-lock` / `sbx upgrade --versions-lock`
  / `SBX_VERSIONS_LOCK` for rollback and reproducible rebuilds.
  `python -m runtime.image --resolve-versions` resolves + freezes +
  prints the evidence JSON (no Modal); `--manifest` and `image_manifest()`
  carry a per-provider `resolution` block (requested vs resolved,
  provenance, evidence). Every image of one deployment receives the same
  frozen spec — rendered Dockerfiles and built images never carry a
  floating `@latest`, so sandbox starts never install `latest`.

## [0.1.1] - 2026-09-19

Public-alpha patch release (release tag `v0.1.1`). Control-plane and
bootstrap feature work on top of `v0.1.0-alpha`; provider pins, adapters
and the frozen contracts (`docs/contracts/*`, `control/backend.py`,
`control/ports.py`, `runtime/runner/adapter.py`) are unchanged, so the
`v0.1.0-alpha` RC-plane provider evidence carries over — no new
real-account gates were run for this tag.

### Added

- First-class git/PR workflow on `/v1` (SOR-128): a `git` policy on
  create-agent declares the work `branch` plus `push` /
  `auto_create_pr` (`target`, `draft`, `title`);
  `POST /v1/agents/{id}/git/publish` pushes the recorded head and can
  open a PR through the opt-in GitHub bridge; `pull_request` is accepted
  as a handoff pin and `POST /v1/agents/{id}/workspace/review` records a
  reviewer verdict on the pinned head.
- Optional `output_contract` on create-agent / create-run (SOR-130): a
  JSON Schema (enforced subset) the run's final message must satisfy.
  `strict` (default) turns invalid output into run `ERROR` with
  `contract_violation` — never a silent success; `warn` keeps `FINISHED`
  with the same diagnostic attached. Verdicts persist on the terminal
  run as `output_contract` + `structured_output`.
- Per-agent session resources (SOR-129): `resources.secrets` attaches
  allowlisted Modal Secrets (`SBX_RESOURCE_SECRETS`) to that agent's
  sandbox only; `resources.mcp` resolves MCP server registry entries
  (`SBX_MCP_REGISTRY`) into the sandbox with `${env:VAR}` indirection —
  names, never values. MCP refs on providers without an MCP channel fail
  `unsupported`; unknown/disallowed refs fail `invalid_resource`.
- Validation evidence artifacts (SOR-131): durable, content-addressed
  evidence items bound to the workspace head that produced them —
  sha256-verified, secret-scanned like workspace artifacts, size-capped,
  and stored separately so handoffs never see them as patch payloads.
- Prepared-environment snapshot cache (SOR-127): a content-derived
  `environment_key` (repo, base_ref, base_sha, image, workdir, setup)
  names each cached environment; snapshots are last-known-good, restores
  still prove `base_sha` fail-closed, and build sandboxes carry no
  credentials (pre-snapshot scrub of credential-shaped paths).
- Automatic OAuth credential write-back (SOR-147): after each turn (and
  once more at session close) the control plane execs
  `runner export-credentials` in the session sandbox and, when the
  provider CLI rotated its tokens, commits the refreshed blob to the
  account credential lane and recreates the managed `sbx-acct-<id>`
  Secret in place — no redeploy. Commits are fingerprint
  compare-and-swap under a per-account lock, so a stale sandbox can never
  clobber a newer stored credential; an `auth_invalid` turn whose
  credential rotated self-heals (an `invalid` account reactivates, and
  the stale run verdict no longer re-marks it). Official CLI auth files
  stay authoritative; `SBX_CRED_WRITEBACK=0` disables write-back.
- GitHub bridge bootstrap persistence (SOR-133):
  `sbx init --github --github-secret <name>` persists `[github]
  ephemeral` + `secret_name`; deploy/upgrade/doctor/status resolve the
  same view and the named Secret is preflighted like other
  prerequisites. Only the Secret *name* persists — token values never
  touch config, deploy env, or output.

### Changed

- One resolved lifecycle chain (SOR-132 / SOR-134 / SOR-135):
  `SBX_IDLE_TIMEOUT_S` is now *post-session idle retention* only — how
  long an idle agent stays warm for a follow-up (default 300 s). The
  sandbox's own native idle bound is a separate knob,
  `SBX_SANDBOX_IDLE_TIMEOUT_S` (default 1800 s, floored at
  `SBX_TURN_MAX_SECONDS` + `SBX_RUN_GRACE_S` so a running turn is never
  reclaimed by it). `SBX_TURN_MAX_SECONDS` is configurable, and
  `SBX_CREATE_GRACE_S` / `SBX_RUN_GRACE_S` bound create/run staleness —
  the runner bound, native sandbox timers and the reaper all resolve
  from the same values.

### Fixed

- Control-plane deploy now mounts `runtime/` into `CONTROL_IMAGE`, so a
  fresh `sbx deploy` is self-contained instead of failing at remote
  import time (SOR-138).
- Stranded `running` sessions reconcile from on-sandbox turn evidence
  after a control-plane restart/cutover instead of holding
  `turn_in_progress` forever; the reaper runs the same settle so a
  provider success lands `FINISHED`, and still-open runs on closed
  sessions persist `EXPIRED`/`ERROR` rather than dangling `RUNNING`
  (SOR-139).
- `GET /v1/artifacts` on the Modal Dict store now enumerates manifest
  *keys* instead of streaming every value — listing cost scales with
  artifact count, not total artifact bytes (SOR-140).

### Known limitations

All `v0.1.0-alpha` limitations still apply: `/v1` may change before 1.0;
self-hosted single-workspace only; no browser/noVNC layer;
`cost_estimate_usd` is a Modal list-price estimate; provider coverage is
unchanged (codex Stable — RC lane still CREDENTIAL_DEFERRED, devin /
antigravity / grok / opencode Experimental, claude not supported).

## [0.1.0-alpha] - 2026-09-17

First public alpha (release candidate for the `v0.1.0-alpha` tag, pending
Owner final acceptance). Self-hosted orchestration for cloud coding
agents: BYO Modal workspace + BYO official provider subscriptions behind
one `/v1` REST API.

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

- Fresh self-host installs now materialize credentials imported through
  `control.onboarding --modal` into deployment-scoped `sbx-acct-<id>` Modal
  Secrets during `sbx deploy` / `sbx upgrade`. Previously the durable account
  blob existed but the runtime Secret did not, so `sbx doctor` could be green
  while the first real provider run failed `runtime_error: Secret ... not found`.
  `sbx doctor` now verifies every account record's referenced Secret as well.
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
- `sbx smoke` surfaces the canonical `run.error` (`code`/`source`/
  `message`/`retryable`/`retry_after`) on a non-FINISHED terminal —
  `auth_invalid` now names the credential fix instead of a bare
  "ended ERROR" (SOR-119).
- `sbx doctor` / `sbx status` aggregate `/v1/models` by provider/account
  (`devin: 1 account, 2 models`) instead of repeating the provider per
  model, and report live agents against `SBX_MAX_CONCURRENT` —
  `idle`/`running`/`creating` agents each hold a slot until closed —
  with `concurrency_limit` remediation pointing at `DELETE /v1/agents/{id}`
  / scoped `DELETE /v1/workflows/{id}` / raising `deploy.max_concurrent`
  (SOR-119).

### Known limitations

- Alpha: `/v1` may change before 1.0. No hosted SaaS, no multi-tenant
  control plane, no browser/noVNC layer, no real billing
  (`cost_estimate_usd` is a Modal list-price estimate).
- Single workspace: one deployment = one Modal workspace; API keys are
  deployment-scoped (`sbx_<key>`, stored as `sha256` only).
- Lifecycle caps: 30 min idle reclaim (configurable), 4 h hard sandbox
  cap, 15 min per-turn soft cap.
- Sandbox-local files (`events.jsonl`, `inbox/`, `turns/`) are ephemeral;
  durable outcomes are the run ledger and artifacts — export artifacts
  before closing an agent.
- Provider coverage: only codex is Stable in this tag; devin /
  antigravity / grok / opencode are Experimental — all four passed
  real-account Modal gates on the RC plane — while codex's own RC gate
  lane is **CREDENTIAL_DEFERRED** (the workspace ChatGPT token is stale
  and noninteractive refresh fails; restoring the lane needs an
  interactive `codex login` — external, not a product defect; the earlier
  real-Modal suite evidence stands). Claude Code is not supported
  (experimental adapter seam merged but unregistered) — see
  `docs/providers.md` for the honest per-provider status.
