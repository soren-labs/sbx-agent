# SOR-170 — Documentation information architecture & prototype

> **DESIGN ONLY — DO NOT MERGE YET.** This is a documentation IA proposal and
> prototype, not a docs-site implementation. It changes no runtime or
> control-plane behavior. All statements below are grounded in the repository
> at base commit `b1783949432b982291c71c6efee7afea184a638e` (`v0.1.1`,
> origin/main); nothing here claims a capability the codebase does not have.
>
> Companion prototypes in this directory:
> [`nav-prototype.yaml`](nav-prototype.yaml) — concrete nav tree;
> [`landing-wireframe.md`](landing-wireframe.md) — landing page wireframe;
> [`guide-skeleton.md`](guide-skeleton.md) — the per-guide page shape.

## 0. Scope and constraints

- **Audience split.** Two distinct reader classes exist and must not be
  mixed: *operators/integrators* (self-host the control plane, call `/v1`)
  and *contributors* (work on this repo). Today they are blended — the
  README serves both, and frozen engineering contracts sit next to user
  guides. The IA below separates them into **Docs** (operators) and
  **Contracts / Contributing** (engineers).
- **Language.** All user-facing documentation is professional English.
  Several existing contract files and repo-internal docs are written in
  Chinese (`docs/contracts/README.md`, `artifacts.md`, `events.md`,
  `runner-cli.md`, `filesystem.md`, the `api-v1.yaml` description,
  `AGENTS.md`, `control/README.md`, `web/README.md`). These are internal
  engineering artifacts; the plan below keeps them canonical but does not
  surface them as user-facing pages. See [Open questions](#open-questions).
- **Honesty rule.** The docs must carry forward the existing evidence
  policy: Stable/Experimental/Preview claims only with real-account
  evidence; known limitations stated, not hidden.
- **Two API surfaces.** `/v1/*` (public, Bearer) is documented as the API
  reference. `/api/*` (internal, Basic, dashboard-only, no compatibility
  promise) gets exactly one pointer page — never reference docs.

## 1. Top-level navigation and sitemap

Proposed top-level nav (order = display order):

| Nav section | Purpose | Audience |
| --- | --- | --- |
| **Overview** | What it is/is not, architecture summary, support status | All |
| **Get Started** | The single first-user path (§2) | New operators |
| **Guides** | Task-oriented how-tos (§3) | Operators, integrators |
| **Concepts** | Architecture, security model, lifecycle, durability, events | All |
| **API Reference** | `/v1` generated from `docs/contracts/api-v1.yaml` (§4) | Integrators |
| **Reference** | `sbx` CLI, configuration, Modal resources, error catalog, support matrix | Operators |
| **Contracts** | Frozen engineering contracts (existing `docs/contracts/`) | Contributors |
| **Releases** | Changelog, upgrade notes, evidence index | All |
| **Security** | SECURITY.md + credential-handling rules | All |
| **Contributing** | CONTRIBUTING.md + AGENTS.md pointer | Contributors |

### Full sitemap

Status column: **R** = reuse existing content, **S** = split out of an
existing file, **N** = new page to write.

| # | Page | Proposed path | Source | Status |
| --- | --- | --- | --- | --- |
| 1 | Landing / Overview | `index` (site root) | README.md §What it is/is not, §Architecture, boundary block | S |
| **Get Started** | | | | |
| 2 | Prerequisites | `get-started/prerequisites` | README §Quick Start prereqs + `sbx init` toolchain checks | S |
| 3 | Credentials | `get-started/credentials` | README §Credentials + providers.md §import/discovery | S |
| 4 | Deploy | `get-started/deploy` | README §Deploying + deployment.md one-shot path | S |
| 5 | Verify: doctor & smoke | `get-started/verify` | README Quick Start + bootstrap.md `doctor`/`smoke` rows | S |
| 6 | Your first agent & run | `get-started/first-agent` | examples/README.md + `sbx_client.py` (create/watch/wait/followup/cancel) | S |
| 7 | Repo workflow quickstart | `get-started/repo-workflow` | repo-workflow.md §Declaring a workspace + git policy | S |
| **Guides** | | | | |
| 8 | Providers & accounts | `guides/providers-accounts` | providers.md (whole, minus matrix) | S |
| 9 | Agents & runs | `guides/agents-runs` | architecture.md lifecycle + api-v1.yaml agents/runs routes | S |
| 10 | Workspaces & Git | `guides/workspaces-git` | repo-workflow.md + deployment.md §GitHub bridge | S |
| 11 | Review & handoff | `guides/review-handoff` | repo-workflow.md §handoff/review + contracts/artifacts.md §4–5 | S |
| 12 | Artifacts & evidence | `guides/artifacts-evidence` | contracts/artifacts.md + CHANGELOG SOR-131 evidence items | S |
| 13 | Workflows & recovery | `guides/workflows-recovery` | architecture.md §Durable vs ephemeral + `/v1/workflows/*` | S |
| 14 | Admin & API keys | `guides/admin-api-keys` | deployment.md key bootstrap + `/v1/api-keys` + bootstrap.md key rotation | S |
| 15 | Usage & cost | `guides/usage-cost` | `/v1/agents/{id}/usage` + `cost_estimate_usd` caveat + concurrency caps | S |
| 16 | Upgrade, cleanup & troubleshooting | `guides/upgrade-cleanup-troubleshooting` | deployment.md §Upgrade/Uninstall + README §Troubleshooting | S |
| **Concepts** | | | | |
| 17 | Architecture | `concepts/architecture` | docs/architecture.md | R |
| 18 | Security model | `concepts/security-model` | architecture.md §Security + SECURITY.md + credential rules | S |
| 19 | Lifecycle & timeouts | `concepts/lifecycle-timeouts` | deployment.md tunables + README §Known limitations caps (SOR-135 chain) | S |
| 20 | Durable vs ephemeral state | `concepts/durability` | architecture.md §Durable vs ephemeral | S |
| 21 | Canonical events | `concepts/events` | contracts/events.md (needs English rewrite or summary page) | S |
| **API Reference** | | | | |
| 22 | API overview (auth, scopes, errors, SSE) | `api/v1/index` | api-v1.yaml info + x-canonical | S |
| 23 | Agents | `api/v1/agents` | api-v1.yaml `agents` tag | G (generated) |
| 24 | Runs | `api/v1/runs` | api-v1.yaml `runs` tag | G |
| 25 | Workspaces | `api/v1/workspaces` | api-v1.yaml `workspaces` tag | G |
| 26 | Artifacts | `api/v1/artifacts` | api-v1.yaml `artifacts` tag | G |
| 27 | Accounts | `api/v1/accounts` | api-v1.yaml `accounts` tag | G |
| 28 | Meta (`/v1/models`, `/v1/me`, usage, workflows) | `api/v1/meta` | api-v1.yaml `meta` tag + workflow routes | G |
| 29 | Error catalog | `api/v1/errors` | api-v1.yaml `x-canonical.error_subcodes` + `run_error_codes` | G |
| 30 | Internal `/api/*` note | `api/internal` | README §API surfaces + contracts/api.yaml pointer | N (one page) |
| **Reference** | | | | |
| 31 | `sbx` CLI | `reference/sbx-cli` | docs/bootstrap.md | R (rename) |
| 32 | Configuration & env vars | `reference/configuration` | bootstrap.md §Configuration + deployment.md tunables | S |
| 33 | Modal resources created | `reference/modal-resources` | README §Deploying + deployment.md §What gets created | S |
| 34 | Provider support matrix | `reference/support-matrix` | README matrix + providers.md matrix + evidence policy | S |
| 35 | Error codes (CLI + API) | `reference/errors` | bootstrap.md error conventions + repo-workflow.md §Errors + x-canonical | S |
| **Contracts** | | | | |
| 36 | Contracts index | `contracts/index` | docs/contracts/README.md | R |
| 37–42 | filesystem / events / runner-cli / api.yaml / api-v1.yaml / artifacts | `contracts/*` | existing files | R |
| **Releases** | | | | |
| 43 | Changelog | `releases/changelog` | CHANGELOG.md | R |
| 44 | Gate evidence index | `releases/evidence` | docs/reviews/* (index page only, files stay in repo) | N (thin) |
| **Security / Contributing** | | | | |
| 45 | Security policy | `security` | SECURITY.md | R |
| 46 | Contributing | `contributing` | CONTRIBUTING.md + AGENTS.md link | R |

## 2. First-user path (zero → first repo workflow)

One linear path under **Get Started**; each page ends with a "you should see
X" checkpoint and a "if not, go to Guide Y" escape hatch. All commands below
exist today at the base commit.

| Step | Page | What the user does | Success signal | Source |
| --- | --- | --- | --- | --- |
| 0 | Landing | Understands the boundary: BYO Modal, BYO provider subscriptions, no hosted accounts | — | README boundary block |
| 1 | Prerequisites | Installs Python ≥ 3.12, `uv`, git; creates a Modal account; clones repo; `uv sync`; `uv run modal token new` (or `MODAL_TOKEN_ID`+`MODAL_TOKEN_SECRET` pair — partial pair is an explicit failure) | `uv run sbx init` toolchain checks pass | README Quick Start |
| 2 | Credentials | Logs into each provider CLI locally (`codex login`, `devin`, `agy`, `grok login`, `opencode auth login`); runs `sbx credentials --verify`; imports blobs via `control.onboarding --modal import` (and `modal secret create sbx-codex-auth` iff codex enabled) | each provider reports `verified` | providers.md, README §Credentials |
| 3 | Deploy | `uv run sbx deploy` — preflight → Secrets → Dicts → images → app → `/v1/me` probe | prints `SBX_BASE_URL`, mints `sbx_<key>` into `bootstrap.key` (0600) | deployment.md |
| 4 | Verify | `uv run sbx doctor` then `uv run sbx smoke --provider <p>` | doctor all green; smoke run reaches `FINISHED` and cleans up | bootstrap.md |
| 5 | First agent & run | `export SBX_BASE_URL/SBX_API_KEY`; `examples/sbx_client.py "Write hello.txt containing hi"`; then programmatically: `create` → `watch` (SSE, `Last-Event-ID` resume) → `wait` → `followup` → `cancel`/`DELETE` | durable terminal status; usage visible via `GET /v1/agents/{id}/usage` | examples/README.md |
| 6 | Repo workflow | `POST /v1/agents` with `workspace:{repo,base_ref,base_sha}` — public repo first (no GitHub auth); then optional `git` policy (branch/push/auto_create_pr), artifact handoff to a second agent, `workspace/review` pin | `WorkspaceRecord` shows `checkout_sha==base_sha`; artifact downloads verify; PR opens only when bridge armed | repo-workflow.md |

**Explicitly deferred on the first path** (linked, not taught): private-repo
GitHub bridge, multi-account fleets, MCP/secret session resources,
`output_contract`, edge Worker deploy, uninstall. The path optimizes for
"one provider, one account, public repo" — the cheapest real proof.

## 3. Task-oriented guides

Each guide follows the same skeleton (see
[`guide-skeleton.md`](guide-skeleton.md)): *Goal → Before you start → Steps
→ Verify → Errors you may hit → Related*. Commands and endpoints listed are
the guide's contract with the reader — all exist at the base commit.

| Guide | Covers | Entry points | Primary sources |
| --- | --- | --- | --- |
| **Providers & accounts** | Support matrix interpretation; local login → `sbx credentials --verify` → import (CLI or `POST /v1/accounts`); fleets via `SBX_<PROVIDER>_ACCOUNTS`; slots/cooldown/failover; `POST /v1/accounts/{id}/verify` probes; OAuth write-back (SOR-147) | `sbx credentials`, `control.onboarding`, `/v1/accounts*` | providers.md |
| **Agents & runs** | Agent lifecycle (`creating→idle⇄running→closed/timed_out/lost`); create/list/get/delete; follow-up runs on native session resume; SSE watch + `Last-Event-ID`; cancel semantics (SIGTERM→30s→SIGKILL); `output_contract`; `resources.secrets`/`resources.mcp`; `idle_timeout_s` | `/v1/agents`, `/v1/agents/{id}/runs*` | api-v1.yaml, architecture.md |
| **Workspaces & Git** | `workspace` declaration; `base_sha` pinning (`base_sha_mismatch` fail-closed); public vs private repos; `git` policy (branch/push/`auto_create_pr`); `POST .../git/publish` + `ls-remote` verification; GitHub bridge arming (env or named Secret) and least-privilege PAT guidance | `/v1/agents` `workspace`/`git`, `/v1/agents/{id}/git/publish` | repo-workflow.md, deployment.md §bridge |
| **Review & handoff** | `handoff` refs (`artifact_id` / `head_sha` / `pull_request`) with the ordered validation chain; review pinning (`reviewed_head_sha`, `head_sha_mismatch`); review-as-comment-only rule (no self-approval path); chained handoffs (B.base_sha == A.head_sha) | `/v1/agents/{id}/handoff`, `/workspace/review` | repo-workflow.md, contracts/artifacts.md |
| **Artifacts & evidence** | Artifact package format (manifest/patch.diff/repo.bundle/files), sha256 on write+read, secret-scan fail-closed (`artifact_secret`), member download, post-teardown reads; validation evidence items (SOR-131) bound to workspace head | `/v1/agents/{id}/artifacts`, `/v1/artifacts*` | contracts/artifacts.md, api-v1.yaml |
| **Workflows & recovery** | `metadata.workflow_id` bindings; `GET /v1/workflows/{id}` recovery view keyed `(key id, workflow_id)`; scoped cleanup `DELETE`; `client.recover`/`close_workflow`; stranded-run reconciliation and `EXPIRED` vs `lost` | `/v1/workflows/{id}` | api-v1.yaml, CHANGELOG SOR-139 |
| **Admin & API keys** | Bootstrap key minting; `sha256`-only storage; `POST /v1/api-keys` (plaintext once); `agents` vs `admin` scopes; revocation; rotation via `sbx deploy` self-heal; deployment-scoped keys | `/v1/api-keys`, `/v1/me`, bootstrap.key | deployment.md, bootstrap.md |
| **Usage & cost** | `GET /v1/agents/{id}/usage` (`input/cached_input/output_tokens`, `sandbox_seconds`, `cost_estimate_usd`); **explicit caveat: Modal list-price estimate, not billing**; concurrency caps (per-key 2 / global 8), idle agents holding slots, `429` remediation | `/v1/agents/{id}/usage`, `sbx status` | api-v1.yaml, deployment.md |
| **Upgrade, cleanup & troubleshooting** | `sbx upgrade` (Dict snapshot → redeploy → verify); `uninstall` + `--purge-data`/`--purge-credentials`; `modal sandbox list` leftover check; symptom→fix table (401, `auth_invalid`, `provider_exhausted`/`concurrency_limit`, `repo_unavailable`, stuck `creating`, SSE stalls) | `sbx upgrade/uninstall/doctor`, `DELETE /v1/agents/{id}` | deployment.md, README §Troubleshooting |

## 4. API reference strategy

**Single source of truth: `docs/contracts/api-v1.yaml`** (OpenAPI 3.1,
frozen per release). Strategy:

1. **Keep the YAML canonical in-repo**; generate the reference site from it
   at publish time (any OpenAPI renderer — the choice of docs generator is
   an open question, §8). No hand-maintained endpoint tables that can drift.
2. **Page per tag, one page per operation group** (sitemap rows 23–28).
   The yaml already carries `operationId`, `summary`, `tags`, structured
   errors, and `x-canonical` vocab — the renderer groups on those.
3. **Auth & envelope page first** (`api/v1/index`): Bearer `sbx_<key>`,
   scopes (`agents` default / `admin`), `{error:{code,message,retry_after?}}`
   envelope, SSE frame semantics (`id:`/`event:`/`data:`, 15 s keepalive,
   `Last-Event-ID`), idempotency codes.
4. **Error catalog page generated from `x-canonical`** —
   `error_subcodes`, `run_error_codes`, `run_error_sources` rendered as a
   table with the fix hints already documented in repo-workflow.md §Errors
   and README §Troubleshooting.
5. **Cross-link, don't duplicate:** prose guides link to reference pages
   (`guides/workspaces-git` → `api/v1/workspaces`); reference pages link
   back to guides for narrative. `examples/sbx_client.py` +
   `examples/README.md` field-mapping stay as the "SDK" section under
   API overview.
6. **`/api/*` is not in the reference.** One page (`api/internal`) states
   it exists, is Basic-auth, dashboard-only, frozen, and unsupported for
   new integrations — pointer to `contracts/api.yaml` for engineers.
7. **Versioning:** the contract is versioned with the release tag; the
   published reference labels itself `v0.1.1` and carries the "may change
   before 1.0" banner from the yaml description. Per-version publishing is
   deferred (open question).
8. **Language gap flagged:** `api-v1.yaml`'s `info.description` is currently
   Chinese — rendering it verbatim would violate the English-only
   requirement. Mitigation options in Open questions; preferred: the site
   supplies its own English overview text and renders only operation-level
   fields (summaries are already English).

## 5. Existing markdown disposition

| File | Disposition | Detail |
| --- | --- | --- |
| `README.md` | **Split** | Boundary/architecture → Overview + Concepts; Quick Start → Get Started 1–5; Provider matrix → Reference/support-matrix; Deploying table → Reference/modal-resources; Credentials → Get Started 2 + guide; Troubleshooting → guide 16. README keeps title, one-paragraph summary, status badge, and links into the docs site — it stops being the docs. |
| `docs/architecture.md` | **Reuse** | `concepts/architecture` nearly as-is; §Durable vs ephemeral and §Security model also seed concept pages 18/20. |
| `docs/bootstrap.md` | **Rename** | → `reference/sbx-cli`; already the per-command reference. |
| `docs/deployment.md` | **Split** | One-shot path → Get Started 4; resource table → Reference 33; tunables → Reference 32 + Concepts 19; GitHub bridge → guide 10; manual path + edge → reference appendix; upgrade/uninstall → guide 16. |
| `docs/providers.md` | **Split** | Matrix + evidence policy → Reference 34; account/credential lifecycle + per-provider notes → guide 8. |
| `docs/repo-workflow.md` | **Split** | → guides 10 + 11. |
| `docs/contracts/*` | **Reuse in place** | Stay frozen, listed under Contracts nav. Chinese-language files remain canonical engineering contracts; public docs paraphrase them in English concept/guide pages rather than publishing them as user docs. |
| `docs/reviews/*` | **Retire from nav** | Evidence stays in-repo; only an index page (row 44) links them. Not user documentation. |
| `AGENTS.md` | **Reuse** | Contributor-facing; linked from Contributing. (Internal working agreement, Chinese — stays as-is; see open questions.) |
| `CONTRIBUTING.md` | **Reuse** | `contributing` page. |
| `SECURITY.md` | **Reuse** | `security` page. |
| `examples/README.md` | **Reuse** | SDK/field-mapping under API overview. |
| `control/README.md`, `web/README.md` | **Retire from nav** | Repo-internal dev docs; reachable from Contributing. |
| `CHANGELOG.md` | **Reuse** | `releases/changelog`. |

Nothing is deleted at this stage; "retire" means *out of the user docs
nav*, not removed from the repo.

## 6. Landing-page wireframe

Full wireframe: [`landing-wireframe.md`](landing-wireframe.md). Section
order: hero + status badge → boundary callouts (BYO Modal / BYO
subscriptions / no hosted accounts) → three-step Quick Start preview →
provider support matrix (abbreviated, linking to full reference) →
`/v1` vs `/api` explainer card → guide cards grid → releases/security
footer strip.

## 7. Docs-only clean-room acceptance checklist

For the eventual docs-site PR(s), not this design PR. A reviewer in a clean
environment (fresh clone, no chat history, no Modal access needed for
reading) verifies:

- [ ] **Every nav item resolves** — no dead links; every sitemap row maps to
  a real page or an explicitly-marked stub.
- [ ] **First-user path is self-contained** — a reader can go zero → smoke
  pass using only the docs; no step requires reading source code or
  another doc outside the path (escapes are links, not requirements).
- [ ] **No invented capability** — every command (`sbx *`, `control.onboarding`,
  `modal secret create`, `make *`) and every endpoint/field cited exists at
  the release commit; every error code appears in `x-canonical` or a
  contract file.
- [ ] **Honest status** — provider matrix matches `docs/providers.md`
  evidence policy verbatim; `cost_estimate_usd` is always labeled an
  estimate; alpha/`/v1`-may-change banner present.
- [ ] **English only** — all user-facing pages, nav labels, error/empty
  states, and generated reference prose are English; code identifiers and
  existing contract files keep their names/content verbatim.
- [ ] **No secrets** — no real tokens; credential examples use placeholders;
  docs never print secret values (matches `sbx doctor` contract).
- [ ] **`/v1` vs `/api` boundary** — no page documents `/api/*` as public;
  `/api` appears exactly once as an internal-pointer page.
- [ ] **API reference provenance** — generated pages trace to
  `docs/contracts/api-v1.yaml` at the tagged commit; version label present.
- [ ] **Troubleshooting coverage** — every `run_error_codes` /
  `error_subcodes` entry has either a guide section or an error-catalog row
  with a fix hint.
- [ ] **Disposition table executed** — each row of §5 is done; retired files
  still exist in-repo and are linked from Contributing/evidence index.

## Open questions

1. **Docs platform** — MkDocs Material / Docusaurus / plain GitHub? The nav
   prototype is YAML-shaped for MkDocs but tool-agnostic. Needed before the
   generated API reference choice.
2. **Contract-file language** — `docs/contracts/*` (and the `api-v1.yaml`
   `info.description`) are Chinese. Options: (a) leave frozen + write English
   paraphrase pages (current proposal); (b) translate contracts via the
   contract-change process — needs an Issue comment, not this PR.
3. **Versioned docs** — publish per release tag, or latest-only until 1.0?
4. **Hosting domain** — README points at repo today; is a docs subdomain /
   the Cloudflare edge a target, or GitHub-rendered only?
5. **README end-state** — confirm README should slim to a pointer page
   (proposal above) vs. staying self-contained with the site mirroring it.
6. **`docs/reviews/` indexing** — is a public evidence index wanted at all,
   or should gates stay repo-internal and only the matrix summarize them?
