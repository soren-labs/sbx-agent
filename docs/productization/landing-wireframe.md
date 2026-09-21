# SOR-170 prototype — landing page wireframe (Markdown)

> DESIGN ONLY. Wireframe of the docs-site landing page (`index`).
> Bracketed `[…]` = link targets in the proposed nav; see docs-ia.md §1.
> Copy below is adapted from existing README text, trimmed for a landing page.

---

# sbx-browser

**Self-hosted orchestration for cloud coding agents.**
`v0.1.1 — public alpha`

One `POST /v1/agents` call creates an isolated Modal Sandbox running the
official provider CLI you already pay for — multi-turn, streaming, with
durable runs, artifacts and workflow recovery.

[ Get Started → ] [ API Reference → ]   *(primary buttons)*

---

## Before you deploy — the boundary   *(three-card callout row)*

| 🖥 BYO Modal | 🔑 BYO subscriptions | 🚫 No hosted accounts |
| --- | --- | --- |
| Control plane and sandboxes run in *your* Modal workspace, billed to you. No hosted service. | Agents authenticate with *your* provider accounts (Codex, Devin, Antigravity, Grok, OpenCode). Subscription quota is never converted into a model API. | Credentials live in your workspace as Modal Secrets/Dict blobs and are injected into your sandboxes only — never logged, never proxied. |

---

## Zero to a running agent in four commands   *(Quick Start preview strip)*

```bash
uv sync && uv run modal token new       # 1. toolchain + your Modal auth
uv run sbx init --providers codex       # 2. config + credential discovery
uv run sbx deploy && uv run sbx doctor  # 3. deploy + verify
uv run sbx smoke --provider codex       # 4. one real run, end to end
```

[ Full first-user path → `get-started/prerequisites` ]

---

## What you get   *(feature card grid, 2×3)*

- **Durable runs** — terminal states persist in `modal.Dict` across sandbox
  teardown and control-plane restarts. [Concepts → durability]
- **Canonical event stream** — one SSE event shape across all providers,
  `Last-Event-ID` resume. [Concepts → events]
- **Repo-native work** — pinned `base_sha` workspaces, git policy
  (branch/push/auto-PR), sha256-verified artifact handoffs.
  [Guide → Workspaces & Git]
- **Multi-account scheduling** — fleets per provider, LRU `account_id:"auto"`,
  cooldown on `auth_invalid`/`rate_limited`. [Guide → Providers & accounts]
- **Workflow recovery** — rebuild an entire workflow from API key +
  `workflow_id`. [Guide → Workflows & recovery]
- **Usage accounting** — per-agent tokens, sandbox seconds, Modal
  list-price estimate. [Guide → Usage & cost]

---

## Provider support at a glance   *(abbreviated matrix — honesty preserved)*

| Provider | Status | Notes |
| --- | --- | --- |
| codex | **Stable** | pinned CLI, real-Modal E2E suite |
| devin · antigravity · grok · opencode | Experimental | merged + real-account gate evidence |
| claude | Not supported | adapter seam merged, unregistered |

[ Full matrix + evidence policy → `reference/support-matrix` ]

---

## Two API surfaces   *(explainer card)*

`/v1/*` is the public, versioned contract — Bearer `sbx_<key>`, documented
endpoints, structured errors. `/api/*` is the internal dashboard API — one
shared Basic credential, no compatibility promise. **Integrate against
`/v1` only.** [API overview → `api/v1/index`]

---

## Footer strip

Changelog · [Release evidence] · [Security policy] · [Contributing] ·
License: pending owner decision
