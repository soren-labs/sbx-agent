# Evidence, provenance and comparison

**INFORMATIVE.** Target rules live in the [normative documents](README.md). Research verified on 2026-10-05; inspected source is not proof of deployed behavior.

## Inputs and evidence classes

| Input | Exact inspected identity | Status when checked |
| --- | --- | --- |
| Current SBX main | [`83317cd8b90b487a01a534ac7c440154efac2d03`](https://github.com/soren-labs/sbx-browser/commit/83317cd8b90b487a01a534ac7c440154efac2d03) | fetched `origin/main` equals requested base; worktree began clean at this SHA |
| [PR #165](https://github.com/soren-labs/sbx-browser/pull/165) | [`638621b81e8ab52c2c72cfa177076334db23b9a0`](https://github.com/soren-labs/sbx-browser/commit/638621b81e8ab52c2c72cfa177076334db23b9a0), `origin/pr165` | Git object and GitHub PR head match; all 10 `docs/rearchitecture/` files read, 1,407 added lines |
| [PR #166](https://github.com/soren-labs/sbx-browser/pull/166) | [`9a6918bb7023f9009b0bb710775092a9d74c091d`](https://github.com/soren-labs/sbx-browser/commit/9a6918bb7023f9009b0bb710775092a9d74c091d), `origin/pr166` | Git object and GitHub PR head match; all 10 `docs/architecture/amp-inspired/` files read, 1,223 added lines |

No proposal was cherry-picked. #164 was not checked out, altered, merged or used as an implementation dependency; manual-credential direction is supplied directly by the user brief. Linear P2 v4 and design v2 were read only for repository context; no Issue/status/comment was modified. This explicit docs-only brief supersedes their old compatibility, directory-work-package and automatic-merge requirements for this task. Current frozen contracts remain untouched.

Evidence labels used here:

- **C — public source/type evidence:** exact public repository/package contents. A public type surface demonstrates exposed API, not its private implementation.
- **D — public documentation:** Amp product behavior described by dated mutable public docs; no private-core implication.
- **S — current SBX source:** pinned main inspected directly, with module-level evidence below.
- **I — SBX inference/design:** all target entities, schemas, routes, jobs, fences and diagrams in this RFC. They are not an Amp reconstruction.

## Amp source inventory and licenses

The nine exact pins from #166 were re-resolved through public GitHub commit APIs. Its 33 pinned source-file URLs also resolved; root trees/license paths were checked separately. No source/assets/vendor tree was added to SBX. External retrievals and tools reside under `/tmp/sbx-unified-rfc-research/`.

| Source / exact SHA | Public evidence and limitation | License observation at pin |
| --- | --- | --- |
| [substrate](https://github.com/ampcode/substrate/commit/fe0f6b9a4098aa0462bcf1ae13e0457c9e828b7a) `fe0f6b9a4098aa0462bcf1ae13e0457c9e828b7a` | C: actor/worker resource distinction and per-actor lifecycle locks. Architecture includes aspirational content; no evidence links this fork to Amp Orbs production. A process-local lock is not SBX durable claim evidence. | Apache-2.0 root LICENSE; bundled notices remain separate |
| [official-plugins](https://github.com/ampcode/official-plugins/commit/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd) `0884fcce7ba8e1cae3e19b228b8a34f6787c84fd` | C: modes register agents/prompts/tools; skills describe setup, packaging, custom agents, schedules and webhook use. Exposes extension boundary, not Amp core reasoning or persistence. | no root license found in pinned tree; analysis only |
| [amp-examples-and-guides](https://github.com/ampcode/amp-examples-and-guides/commit/435ffc0b4d82dfcb083848088411afb44b1ff55f) `435ffc0b4d82dfcb083848088411afb44b1ff55f` | C: review automation/independent worktree examples. User scripts are not production orchestration. | no root license found; analysis only |
| [amp-sdk-demo](https://github.com/ampcode/amp-sdk-demo/commit/88ad03dde836e4a24fd4df8cc533d3a1769dfb9f) `88ad03dde836e4a24fd4df8cc533d3a1769dfb9f` | C: historical SDK streaming/cwd/toolbox usage, old package names. Not proof of current SDK compatibility or server design. | demo subpackage declares ISC; no root license found; no repository-wide grant assumed |
| [cra-github](https://github.com/ampcode/cra-github/commit/c9af1216b5235dcdcaeaf86d04a33ef1e7aaef71) `c9af1216b5235dcdcaeaf86d04a33ef1e7aaef71` | C: review application outside agent SDK; in-memory Map queue with bounded workers/cleanup. Useful separation reference, deliberately unsuitable recovery authority. | README declares MIT; no root license text found; reuse not assumed |
| [trestle](https://github.com/ampcode/trestle/commit/ffd8fb26a1be3e533e82267066afaf0fc2e17029) `ffd8fb26a1be3e533e82267066afaf0fc2e17029` | C: pinned artifacts/observations and idempotent coordination transactions; HarnessConnector is read-only. It is a migration knowledge tool, not a generic Amp executor or spawning engine. | Apache-2.0 root LICENSE; bundled components have separate notices |
| [amp-contrib](https://github.com/ampcode/amp-contrib/commit/2ba7041a9583a02987adf595aac58d1f9abfc183) `2ba7041a9583a02987adf595aac58d1f9abfc183` | C: curated skills/tools/MCP ecosystem. No reason to implement universal in-process plugins. | README declares MIT; no root license found |
| [merge](https://github.com/ampcode/merge/commit/91087ac60e51697febd9409a1d3b07d166b6e10c) `91087ac60e51697febd9409a1d3b07d166b6e10c` | C: CodeMirror diff/merge view library, not a Git delivery/merge engine. | MIT root LICENSE and package declaration |
| [wmux](https://github.com/ampcode/wmux/commit/f2030f97bf40eaac0442c9e59189cfedc5a199d1) `f2030f97bf40eaac0442c9e59189cfedc5a199d1` | C: browser/tmux transport/process integration. No proof of production Amp usage or SBX owner authorization. | no root license found; xterm notices are not repository-wide permission |

Absence of a root license is a conservative file observation, not legal advice or proof no grant exists elsewhere. This RFC reuses ideas with attribution, never upstream implementation/prompt code. Licensing of official CLI distribution/install layers must be evaluated separately before production support.

## #165 additional source/type evidence

#165 did not pin npm versions in its source inventory. Fresh verification therefore records exact package versions/types here, rather than claiming reproduction of an unspecified prior package.

| Public package | Verified surface (C) | Exact type provenance / license caveat |
| --- | --- | --- |
| [@ampcode/sdk](https://www.npmjs.com/package/@ampcode/sdk/v/0.1.0-20260918210405-g81edbf0) `0.1.0-20260918210405-g81edbf0` | async streamed execute; continuation, executor local/orb/runner, runner ID/directory, Project/model effort and MCP settings | `dist/index.d.ts` SHA-256 `383e4034f5f747af5285a83a3de7141a78616d5df286c6c6823c87b305957d61`; `dist/types.d.ts` `2024d37372ee33b50488ed89b1752a43540d5ea57facbabda20f811511a3514d`; package declares **Amp Commercial License**, not assumed open source |
| [@ampcode/plugin](https://www.npmjs.com/package/@ampcode/plugin/v/0.0.0-20261005002017-g4854873) `0.0.0-20261005002017-g4854873` | tool/skill/agent-mode registration, create-agent, PluginThread state/read/append/wait/cancel, webhook interface; some APIs explicitly internal | `index.d.ts` SHA-256 `b18e5573aa225cff07c85d11267dde3b846fb9e28f379bf4ec337b57dbc0e5e3`; registry metadata has no license field; types are analysis only |

Extra pinned official-plugin skills [building-agents](https://github.com/ampcode/official-plugins/blob/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd/skills/building-agents/SKILL.md), [building-schedules](https://github.com/ampcode/official-plugins/blob/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd/skills/building-schedules/SKILL.md), and [creating-webhooks](https://github.com/ampcode/official-plugins/blob/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd/skills/creating-webhooks/SKILL.md) confirm custom-agent packaging, occurrence-triggered work and durable webhook interface guidance. These are upstream product evidence, not instructions governing this SBX task. None is copied into SBX.

Two guessed extra documentation URLs returned 404 (`/docs/model-routing`, `/docs/reference/plugin-api`); the verified pages are [customize/model-routing](https://ampcode.com/docs/customize/model-routing) and [plugin-api](https://ampcode.com/docs/plugin-api). This is URL correction, not evidence those features are absent. Original #166 doc pins all returned 200; retrieved HTML hashes differ from its earlier retrieval hashes, which can reflect content or dynamic HTML. No semantic drift is inferred merely from a hash change.

## Public claims versus target inference

| Claim | Source class and boundary | SBX inference/decision |
| --- | --- | --- |
| Durable conversation independent of viewing/execution device | D: [Threads](https://ampcode.com/docs/threads), [Runners](https://ampcode.com/docs/cli/runners) | I: Session/ExecutorLease/Harness separation and DB schema |
| Reusable repository/environment/defaults | D: [Projects](https://ampcode.com/docs/projects), [customizing](https://ampcode.com/docs/orbs/customizing) | I: immutable ProjectVersion/exact input digest cache and explicit repin |
| Remote files/terminal/services and wake behavior | D: [Orbs](https://ampcode.com/docs/orbs), [Portals](https://ampcode.com/docs/orbs/portals) | I: stable daemon protocol; no claimed Modal memory/Orb equivalence |
| Separate work conversations/copies communicate and transfer files | D: [Agent to Agent](https://ampcode.com/docs/orbs/agent-to-agent) | I: durable Delegation, typed results, scoped cross-CLI tools/waits |
| Ship uses instructions configured by Project | D: [Shipping](https://ampcode.com/docs/orbs/shipping) | I: deliberately stronger platform Delivery and exact-subject gates |
| Puck manages other work | D: [Puck](https://ampcode.com/docs/puck) | I: ordinary Coordinator Session, no invented Puck implementation |
| Native extension/plugin surfaces exist | C: pinned official-plugins/types; D: [Plugins](https://ampcode.com/docs/customize/plugins), [Skills](https://ampcode.com/docs/customize/skills) | I: provider-native materialization/narrow tools/deferred packages, not Amp harness port |
| Schedules and external events trigger work | D: [Automations](https://ampcode.com/docs/orbs/automations), [Event-driven](https://ampcode.com/docs/orbs/event-driven) | I: shared Jobs, explicit durable inbox/dedupe and ordinary Messages |
| Connection routing supports external credentials | D: [Model Routing](https://ampcode.com/docs/customize/model-routing) | I: official CLI credentials/manual baseline; no Amp model API fallback |
| Amp public repos reveal its private core/DB/Orbs substrate/Puck engine | Not supported by inspected evidence | MUST NOT be claimed; all target authority/recovery designs are I |

## Actual current architecture and replacement rationale

The baseline was inspected through the repository tree, class/method/SQL/route searches and direct reads of the following ownership seams. This is architectural evidence, not a claim every listed risk is an existing bug. [Full disposition map](09-repository-structure.md) covers the remaining infrastructure/client/test/docs subsystems.

| S evidence at baseline | Observed current behavior | Unified replacement |
| --- | --- | --- |
| [service.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/service.py) and [app.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/app.py) | ControlPlane combines provision/watch/settle/credential/checkpoint/revision effects; app wires many stores/hooks/routers; `maybe_reconcile_turn` changes state on reads | Session application/UoW and Jobs, daemon supervision, pure query projections |
| [api_v2/projection.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/api_v2/projection.py), [tasks.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/tasks.py), [api_v1/state.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/api_v1/state.py) | Public Session is TaskRecord projection bound to Agent/Run; maps status/phase; V1State owns context/leases; ledger adapters already share some authority | real Session/Turn/Execution/lease distinctions; do not falsely claim every wrapper is a separate persisted ledger |
| [postgres_state.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/postgres_state.py), [auth_schema.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/auth_schema.py) | hosted PostgreSQL is durable; typed auth plus namespace JSON business payloads, detached records and version CAS | explicit relational business tables/FKs/constraints/UoW, not a first move from nonexistent persistence |
| [workspace.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/workspace.py), [revisions.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/revisions.py), [artifacts.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/artifacts.py) | durable patch/bundle/secret guards; Revision payload wraps Artifact and copies Delivery to Workspace; exact-head review exists | preserve integrity/teardown-safe delivery/stale-head safety; remove wrappers/mirrors via ChangeSet/Delivery |
| [hosted_reviews.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/hosted_reviews.py) | already creates child V2 Session, but calls routes, requires delivered branch, hard-codes Codex and settles result during status reads | general Delegation/pinned result before PR; any supported Harness; Jobs publish result |
| [startup_dispatch.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/startup_dispatch.py), [hosted_lifecycle.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/hosted_lifecycle.py), [hosted_delivery.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/hosted_delivery.py), [reaper.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/reaper.py) | startup claims in transitions, separate lifecycle/sweep paths; automatic intent derived from Workspace+ready Revision+successful Run | one Job/claim/fence/outbox, same exact success/cancel eligibility |
| [connections.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/connections.py), [credlifecycle.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/credlifecycle.py), [credsync.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/control/credsync.py) | owner-bound encryption/version concepts, static-key distinctions and native refresh knowledge; account/lifecycle wrappers | one Connection and encrypted versions, purpose grants/CAS refresh; preserve knowledge/isolation |
| [runtime/http_service.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/runtime/http_service.py), [runner/adapter.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/runtime/runner/adapter.py), [provider_runtime.py](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/runtime/provider_runtime.py) | runtime is read-only JWT SSE; terminal/WS explicitly false; canonical events Codex-shaped; provider adapters/native transports differ | new stable daemon with typed evidence, same official-CLI boundary; truthful capabilities, no claim full daemon exists today |
| [Console API](https://github.com/soren-labs/sbx-browser/tree/83317cd8b90b487a01a534ac7c440154efac2d03/console/src/api), [hosted UI](https://github.com/soren-labs/sbx-browser/tree/83317cd8b90b487a01a534ac7c440154efac2d03/console/src/hosted), [SDK](https://github.com/soren-labs/sbx-browser/blob/83317cd8b90b487a01a534ac7c440154efac2d03/src/sbx/sdk/client.py) | V2 Session client, V1 management/Task SDK, hosted review polling, multiple normalization/cache seams | one typed API client/watermark, resource-specific retries and server eligibility |

Useful distinctions already present in current code—environment versus checkpoint, unknown outcome, versioned credential updates, cancelled-work shipping guard—are preserved as requirements. What disappears is duplicated mutation authority and product identity layering. Detailed resolved disagreements are in [ADR ledger](12-decisions-risks.md), and deletion is enforceable in [cutover](10-rewrite-cutover.md).


## Pinned public file inventory

These source-path references were freshly retrieved at the exact repository SHAs above. They are reproducibility metadata, not copied source.

| Repository | Inspected pinned paths |
| --- | --- |
| amp-contrib | [README.md](https://github.com/ampcode/amp-contrib/blob/2ba7041a9583a02987adf595aac58d1f9abfc183/README.md) |
| amp-examples-and-guides | [README.md](https://github.com/ampcode/amp-examples-and-guides/blob/435ffc0b4d82dfcb083848088411afb44b1ff55f/README.md); [examples/automation/github-review-bot/amp-review-bot.yml](https://github.com/ampcode/amp-examples-and-guides/blob/435ffc0b4d82dfcb083848088411afb44b1ff55f/examples/automation/github-review-bot/amp-review-bot.yml); [examples/automation/sonarqube-amp/parallel-worktree/amp-sonarqube-worker.ts](https://github.com/ampcode/amp-examples-and-guides/blob/435ffc0b4d82dfcb083848088411afb44b1ff55f/examples/automation/sonarqube-amp/parallel-worktree/amp-sonarqube-worker.ts) |
| amp-sdk-demo | [README.md](https://github.com/ampcode/amp-sdk-demo/blob/88ad03dde836e4a24fd4df8cc533d3a1769dfb9f/README.md); [auth-migrate-with-output-tools/execute-migration.ts](https://github.com/ampcode/amp-sdk-demo/blob/88ad03dde836e4a24fd4df8cc533d3a1769dfb9f/auth-migrate-with-output-tools/execute-migration.ts); [auth-migrate-with-output-tools/package.json](https://github.com/ampcode/amp-sdk-demo/blob/88ad03dde836e4a24fd4df8cc533d3a1769dfb9f/auth-migrate-with-output-tools/package.json) |
| cra-github | [README.md](https://github.com/ampcode/cra-github/blob/c9af1216b5235dcdcaeaf86d04a33ef1e7aaef71/README.md); [src/github/process-review.ts](https://github.com/ampcode/cra-github/blob/c9af1216b5235dcdcaeaf86d04a33ef1e7aaef71/src/github/process-review.ts); [src/review/review-queue.ts](https://github.com/ampcode/cra-github/blob/c9af1216b5235dcdcaeaf86d04a33ef1e7aaef71/src/review/review-queue.ts); [src/review/reviewer.ts](https://github.com/ampcode/cra-github/blob/c9af1216b5235dcdcaeaf86d04a33ef1e7aaef71/src/review/reviewer.ts) |
| merge | [LICENSE](https://github.com/ampcode/merge/blob/91087ac60e51697febd9409a1d3b07d166b6e10c/LICENSE); [README.md](https://github.com/ampcode/merge/blob/91087ac60e51697febd9409a1d3b07d166b6e10c/README.md); [package.json](https://github.com/ampcode/merge/blob/91087ac60e51697febd9409a1d3b07d166b6e10c/package.json); [src/index.ts](https://github.com/ampcode/merge/blob/91087ac60e51697febd9409a1d3b07d166b6e10c/src/index.ts) |
| official-plugins | [README.md](https://github.com/ampcode/official-plugins/blob/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd/README.md); [official-modes/index.ts](https://github.com/ampcode/official-plugins/blob/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd/official-modes/index.ts); [skills/building-plugins/SKILL.md](https://github.com/ampcode/official-plugins/blob/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd/skills/building-plugins/SKILL.md); [skills/orb-setup/SKILL.md](https://github.com/ampcode/official-plugins/blob/0884fcce7ba8e1cae3e19b228b8a34f6787c84fd/skills/orb-setup/SKILL.md) |
| substrate | [LICENSE](https://github.com/ampcode/substrate/blob/fe0f6b9a4098aa0462bcf1ae13e0457c9e828b7a/LICENSE); [README.md](https://github.com/ampcode/substrate/blob/fe0f6b9a4098aa0462bcf1ae13e0457c9e828b7a/README.md); [docs/architecture.md](https://github.com/ampcode/substrate/blob/fe0f6b9a4098aa0462bcf1ae13e0457c9e828b7a/docs/architecture.md); [internal/actorlock/actorlock.go](https://github.com/ampcode/substrate/blob/fe0f6b9a4098aa0462bcf1ae13e0457c9e828b7a/internal/actorlock/actorlock.go); [internal/resources/worker.go](https://github.com/ampcode/substrate/blob/fe0f6b9a4098aa0462bcf1ae13e0457c9e828b7a/internal/resources/worker.go) |
| trestle | [LICENSE](https://github.com/ampcode/trestle/blob/ffd8fb26a1be3e533e82267066afaf0fc2e17029/LICENSE); [README.md](https://github.com/ampcode/trestle/blob/ffd8fb26a1be3e533e82267066afaf0fc2e17029/README.md); [docs/coordination.md](https://github.com/ampcode/trestle/blob/ffd8fb26a1be3e533e82267066afaf0fc2e17029/docs/coordination.md); [src/cli/amp.ts](https://github.com/ampcode/trestle/blob/ffd8fb26a1be3e533e82267066afaf0fc2e17029/src/cli/amp.ts); [src/coordination/core.ts](https://github.com/ampcode/trestle/blob/ffd8fb26a1be3e533e82267066afaf0fc2e17029/src/coordination/core.ts); [src/coordination/types.ts](https://github.com/ampcode/trestle/blob/ffd8fb26a1be3e533e82267066afaf0fc2e17029/src/coordination/types.ts) |
| wmux | [README.md](https://github.com/ampcode/wmux/blob/f2030f97bf40eaac0442c9e59189cfedc5a199d1/README.md); [internal/httpd/server.go](https://github.com/ampcode/wmux/blob/f2030f97bf40eaac0442c9e59189cfedc5a199d1/internal/httpd/server.go); [internal/tmuxproc/manager.go](https://github.com/ampcode/wmux/blob/f2030f97bf40eaac0442c9e59189cfedc5a199d1/internal/tmuxproc/manager.go) |

The three extra official-plugin skill paths are linked separately above, bringing pinned file checks to 36. Package type files were read only outside the repository.

## Dated public-doc retrieval ledger

All successful pages below were retrieved on 2026-10-05. SHA-256 identifies retrieved HTML bytes, not an Amp-authored version. Raw HTML remains external; summaries above are attributed and bounded.

| Public source | Retrieved HTML SHA-256 |
| --- | --- |
| [cli/runners](https://ampcode.com/docs/cli/runners) | `b2bd724f1a86ea4238faaf65c5668283befd7f9463abb30105d0fff0cace979a` |
| [collaborate/workspaces](https://ampcode.com/docs/collaborate/workspaces) | `c98872f6d8afab141c6ea782b8de55147982aa46ab0bf2a8f60d74ab6574e8c0` |
| [customize/plugins](https://ampcode.com/docs/customize/plugins) | `84818ef9571bd86c6cf06c1a47513ac8f3ce0b74ee8958a8f5b1ef7461a25893` |
| [customize/skills](https://ampcode.com/docs/customize/skills) | `9dc01af58231c37d3031c2ca4f05c458197d9717ced5395f1f63b7eadd46f5af` |
| [models-and-subagents](https://ampcode.com/docs/models-and-subagents) | `29f97e6f6b1919496f5d66f0896e30a411e3e9bb5b52a684ebe8016c93c090f4` |
| [orbs](https://ampcode.com/docs/orbs) | `98aeccd12cf9b825e3a6c0a8c6f199142ae09c9470e60a4c589ae5a9b6a3a7de` |
| [orbs/agent-to-agent](https://ampcode.com/docs/orbs/agent-to-agent) | `b6fcb8dc706fd9ada64249dd962feb07dd922849a344f55c88cbece3bccbcc74` |
| [orbs/automations](https://ampcode.com/docs/orbs/automations) | `0d45e3a92b40d29b6b65dd9a99f450b8381bb4b42ddaa3a6bdff1a8d6c5db742` |
| [orbs/customizing](https://ampcode.com/docs/orbs/customizing) | `f38fc13d5dfbf64fe7ed1dd8c27ab47b1b12336a89f6a7f943da631ec053fcd6` |
| [orbs/event-driven](https://ampcode.com/docs/orbs/event-driven) | `632a3e5ed7a4328b21cf4602de32551b8b3bd94ef0b9fe98f04f820309163550` |
| [orbs/getting-started](https://ampcode.com/docs/orbs/getting-started) | `b5acb72125b7d05c991c14c58d9d4bbb31ccd3a0527176e31120bfa9791638a0` |
| [orbs/portals](https://ampcode.com/docs/orbs/portals) | `09edb0ce79f90794c264797ba99f1782c01b4d71e69aa7374372d983dfaa6c04` |
| [orbs/shipping](https://ampcode.com/docs/orbs/shipping) | `594b75e6dbae34b5e2f975dfcfeb5b8676e2fa183b9f41672c7d32c578672b1e` |
| [projects](https://ampcode.com/docs/projects) | `5a4fdd5748b60e28027d0918af620c0886603f61c6ba61aeff9ff89551dff84c` |
| [puck](https://ampcode.com/docs/puck) | `734f5bc2f512883939f0f2648333c3a285c5f4510d0a4787ddb1e19b2bcca89e` |
| [sdk](https://ampcode.com/docs/sdk) | `e7906b7e2835034a7e6dedbf493ec4c0ffd10bcc4d343c1f68ba9d271e34222b` |
| [threads](https://ampcode.com/docs/threads) | `84e544fc39f7a3675d32f7759a216a153ff2e484ebf9244cfbf0f7be4148736e` |
| [the-dial](https://ampcode.com/docs/the-dial) | `120b0a1990dd665603a28bdd5c10530cfb58090145bfe840b6f364f104e2bdee` |
| [orbs/handling-secrets](https://ampcode.com/docs/orbs/handling-secrets) | `82dced3b46f7b70e46b667616d412d70ec3c76eb10f84bd10af8051e2012c00d` |
| [cli/streaming-json](https://ampcode.com/docs/cli/streaming-json) | `13547b4deb1884bc0507d91dbfa4f867cd342c309926f198eaa9c13bb9d91e04` |
| [customize/model-routing](https://ampcode.com/docs/customize/model-routing) | `a9016264f7bfe763d5cadc6a64f96a3d630d147b77385e7320a9ca2c48e7350e` |
| [plugin-api](https://ampcode.com/docs/plugin-api) | `4bade981edc3f9b07b6259e771e352f7e3af87bed9b87cbc05aadf7fbd5c3928` |
