# sbx Control Center — UI prototype (SOR-171)

**DESIGN ONLY — DO NOT MERGE.** A clickable, mock-data prototype of a human-facing
Control Center for the sbx-browser control plane. It is **not** wired to the
control plane and does not replace `web/` — every value on screen is static mock
data shaped on `docs/contracts/api-v1.yaml` (the public `/v1` mental model:
`agent ≙ session`, `run ≙ turn`).

## Run it

```bash
cd prototype/control-center
npm install
npm run dev          # http://localhost:5173 (any free port works)
```

Production build check: `npm run build` (type-checks + bundles to `dist/`).

No backend is required — routing is hash-based (`#/agents`, …) so the app also
works when served as static files.

## Scenario switcher

The top-right **Scenario** selector flips the whole mock dataset to preview the
same screens under different fleet states:

| Scenario | What it shows |
| --- | --- |
| Healthy fleet | Agents running, accounts verified, mixed statuses |
| Fresh deploy | No agents, no accounts — cold-start empty states |
| All idle | Fleet exists, nothing running |
| Degraded | `auth_invalid` account, cooldowns, lost agents, warnings |

## Screen map

| Route | Screen | What it covers (API shape) |
| --- | --- | --- |
| `#/` | **Overview** | Control-plane `/v1/me` probe, live agents vs `SBX_MAX_CONCURRENT` cap, provider readiness rollup, recent runs feed |
| `#/agents` | **Agents list** | `GET /v1/agents` with `provider` / `status` / `workflow_id` filters, workflow binding badges, token/cost columns, empty state |
| `#/agents/{id}` | **Agent detail** | `GET /v1/agents/{id}` header + status/tokens/cost/workflow cards; tabs: **Runs** (run cards + SSE timeline of canonical events `turn.*`/`item.*`, RunError card, `structured_output` + `output_contract` verdict), **Workspace** (inline `WorkspaceRecord`), **Artifacts**, **Raw JSON**. Actions: follow-up run, cancel, close |
| `#/agents/create` | **Create agent wizard** | 5 steps mapping onto `CreateAgentRequest`: prompt/name/idle_timeout → provider+account (`auto` vs pinned) + model → `workspace` + `git` policy + `handoff` → `output_contract` + workflow `metadata` → review with live `POST /v1/agents` JSON preview and inline validation (`auto_create_pr` requires `push`, handoff requires `workspace`, exhausted accounts → `provider_exhausted`) |
| `#/providers` | **Providers & accounts** | v0.1.1 support matrix (Stable/Experimental, pinned CLI, auth file, evidence), account pool table (`active/cooling/invalid/disabled`, slots, `last_error`, verify/remove actions), `GET /v1/models` availability, credential lifecycle explainer |
| `#/workspace/{id}` | **Workspace, git & handoff** | Full `WorkspaceRecord` (base/checkout/head/reviewed shas), recorded PR metadata, `git/publish` action, `workspace/review` pin form (comment = PR comment, never a GitHub approval), `handoff` form (`artifact_id` / `head_sha` / `pull_request`) with simulated `409` conflicts |
| `#/workflows/{id}` | **Workflows & recovery** | `GET /v1/workflows/{id}` recovery view: progress aggregates (`tasks`, `open_agents`, `latest_runs_by_status`, `all_terminal`), bound-agent table incl. `status: missing`, scoped cleanup (`DELETE`) with the `WorkflowCleanupResult` shape |
| `#/artifacts/{id}` | **Artifacts & evidence** | Artifact list + manifest detail (`payloads` member→sha256, files table, `test_command` exit codes, warnings), per-member downloads, and a **Structured output** tab showing `structured_output` + `output_contract` verdict/violations |
| `#/keys` | **API keys & admin** | `GET /v1/me` identity/scopes, key table (`agents`/`admin` scopes, revoked state), create-key flow with "plaintext shown once" warning, scope guard explanations (401/403) |
| `#/usage` | **Usage, cost & lifecycle** | Per-agent usage table (`input/cached/output/reasoning` tokens, `cost_estimate_usd`, "unmeasured" = `null` not 0), sandbox-hours rollup, lifecycle state diagram (creating→idle→running→closed/timed_out/lost, reaper 5m retention, 4h hard timeout), concurrency-cap guidance |
| `#/states` | **Edge states gallery** | Empty / credential-unready cards plus every canonical HTTP `error.code` (400/401/403/404/409/429 with `retry_after`) and the `run_error_codes` RunError panel |

## Responsive navigation

Sidebar collapses behind a ☰ menu below ~720px; grids reflow to single column
(`screenshots/mobile-overview.png`).

## Screenshots

Captured headlessly against `npm run dev`, in `screenshots/`: `overview`,
`agents`, `agents-ag_7f3k2` (detail + run timeline), `agents-create`,
`providers`, `workspace-ag_7f3k2`, `workflows-wf-release-012`,
`artifacts-art_3q9z`, `keys`, `usage`, `states`, `mobile-overview`.

## Not covered / open questions

- No real data path: a production version would need a `/v1` read surface for the
  dashboard's own key, plus SSE wiring for the run timeline.
- Whether the Control Center is served by `sbx-control` statics (like `web/`),
  the edge worker, or a separate host is intentionally unresolved.
- SSE replay / artifact download UX and multi-key identity are mocked, not
  designed.
