# sbx-browser

**Self-hosted orchestration for cloud coding agents.** One `POST /v1/agents`
call creates an isolated [Modal](https://modal.com) Sandbox that runs the
official provider CLI you already pay for (Codex, Devin, Antigravity, Grok,
OpenCode) — multi-turn, streaming, with durable runs, artifacts and workflow
recovery.

> **Status: `v0.1.0-alpha` (public alpha).** Self-hosted bring-your-own-everything
> release. The `/v1` API may still change; see [Known limitations](#known-limitations).

## What it is — and what it is not

sbx-browser gives you a single REST API (`/v1`, Bearer-authenticated) in front
of per-agent Modal Sandboxes. Each agent is a long-lived sandbox running an
official provider CLI under your own accounts; the control plane schedules
turns across accounts, streams canonical events over SSE, persists run state,
and hands work between agents via artifacts.

**Boundary — read this before deploying:**

- **BYO Modal**: the control plane and all sandboxes run in *your* Modal
  workspace, billed to you. There is no hosted service.
- **BYO subscriptions**: agents authenticate with *your* official provider
  accounts and subscriptions. sbx-browser does **not** resell, proxy, or
  convert subscription quota into a model API — tasks execute inside the
  provider's own CLI/harness, under that provider's terms.
- **No hosted accounts**: sbx-browser does not host user accounts, store
  model weights, or call provider model APIs directly. Credentials you import
  live in your Modal workspace (as Secrets / Dict blobs) and are injected
  into your sandboxes only.

## Architecture

```
 you (client / CI)                     your Modal workspace
 ┌─────────────────┐   HTTPS +        ┌──────────────────────────────────────┐
 │ sbx_client.py   │   Bearer sbx_*   │ sbx-control (FastAPI, scales to 0)   │
 │ curl / your app │ ───────────────▶ │  /v1 API · run ledger · scheduler    │
 └─────────────────┘                  │  account pool · reaper (5 min cron)  │
                                      │  state: modal.Dict sbx-sessions/-    │
                                      │    runs/-accounts/-workflows         │
                                      └───────────────┬──────────────────────┘
                                                      │ Sandbox.create(
                                                      │   image, idle_timeout,
                                                      │   cpu/mem, secrets)
                                                      ▼
                                        ┌── 1 agent = 1 Modal Sandbox ──────┐
                                        │ entrypoint → runner               │
                                        │  init · turn · stop · export-     │
                                        │  credentials                      │
                                        │ official provider CLI             │
                                        │  (codex / devin / agy / grok /    │
                                        │   opencode)                       │
                                        │ credential files only — no        │
                                        │  Modal or platform keys inside    │
                                        └───────────────────────────────────┘
```

- **One agent = one sandbox.** Idle reclamation uses Modal's native
  `idle_timeout`; a hard `timeout` (4 h default) is the backstop.
- **The sandbox is the only security boundary.** Provider CLIs run with
  approvals bypassed *inside* the sandbox; no Modal token or platform
  credential exists inside it.
- **Durable by design.** Run terminal states, workflow bindings and artifacts
  persist in `modal.Dict` and survive sandbox teardown and control-plane
  restarts.

Deep dive: [docs/architecture.md](docs/architecture.md). Frozen interface
contracts (filesystem, events, runner CLI, `/v1` OpenAPI):
[docs/contracts/](docs/contracts/README.md).

## Quick Start

Prerequisites: Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), a Modal
account, and at least one provider CLI logged in locally.

```bash
git clone <this-repo> && cd sbx-browser
uv sync                          # install control-plane + client deps
modal token new                  # authenticate YOUR Modal workspace
```

The release ships a bootstrap CLI (`sbx`, SOR-98) that performs init →
credential import → deploy → health check in one pass:

```bash
sbx init                         # check env, write local config, pick Modal profile
modal secret create sbx-codex-auth \
  CODEX_AUTH_JSON="$(cat ~/.codex/auth.json)"   # your provider credential
sbx deploy                       # build images, init Dicts/Secrets, deploy control
sbx doctor                       # verify auth, secrets, /v1 auth, providers
sbx smoke                        # minimal real run through /v1
```

It prints your `SBX_BASE_URL` and a `sbx_<key>` API key (shown once).

> The equivalent manual steps — `modal secret create …`,
> `make image && make deploy`, key bootstrap via the `sbx-v1-bootstrap`
> Secret, and `python -m control.onboarding --modal import` — are documented
> in [docs/deployment.md](docs/deployment.md).

Then run your first agent:

```bash
export SBX_BASE_URL=<printed by sbx deploy / doctor>
export SBX_API_KEY=sbx_<key>
python examples/sbx_client.py "Write hello.txt containing hi"
```

## Provider Support Matrix

| Provider | Status in 0.1 | CLI / version | Auth material | Multi-turn | Cancel | Multi-account | Real-E2E evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **codex** | **Stable** | `@openai/codex` 0.153.0 (pinned in image) | `~/.codex/auth.json` (ChatGPT login) | ✅ `exec resume` | ✅ | ✅ | Real-Modal suite `tests/e2e_modal/` + committed `timings.json`; P0 spike; RC gate lane CREDENTIAL_DEFERRED (stale ChatGPT token — interactive `codex login` needed, external) |
| **devin** | Experimental | Devin CLI 3000.10.21 (sha256-pinned) | `~/.local/share/devin/credentials.toml` | ✅ via ACP | ✅ | ✅ | Modal clean-room credential + 8-way concurrency PASS, 2026-09-14 (`spike/p2/`); Release 0.1 `/v1` real-Modal gate PASS on the RC plane (`docs/reviews/release-0.1-gate-core.md`) |
| **antigravity** | Experimental | your `agy` binary | OAuth token file | ✅ `--conversation` | ✅ | ✅ | Real-account gate harness `tests/e2e_modal/agy_gate.py`; SOR-68 fleet gate PASS 50/50 on the RC plane (`docs/reviews/release-0.1-gate-agy.md`) |
| **grok** | Experimental | your `grok` binary (verified 1.0.24) | `~/.grok/auth.json` | ✅ `--resume` | ✅ | ✅ | Real-account gate harness `tests/e2e_modal/grok_gate.py`; SOR-68 runner + fleet gates PASS on the RC plane (`docs/reviews/release-0.1-gate-grok.md`) |
| **opencode** | Experimental | `opencode-ai` 1.18.29 (npm-pinned) | `~/.local/share/opencode/auth.json` | ✅ `--session` | ✅ | ✅ `SBX_OPENCODE_ACCOUNTS` | Release 0.1 real-account gate PASS on the RC plane — two turns on one native session, cancel, zero leaks (`docs/reviews/release-0.1-gate-core.md`) |
| **claude** | Not supported | — | — | — | — | — | Experimental adapter seam merged but **not registered** (SOR-97, replay-only); not schedulable |

**Evidence policy.** *Stable* requires a passing real-account E2E on the
release tag, not fakes or replays. *Experimental* means the production adapter,
image and credential path are merged and some real-account evidence exists,
but the full release-gate matrix has not completed for that provider.
*Preview* means the production adapter, image and credential path are merged
but no real-account evidence exists yet — replay/fixture coverage only, so
the provider is usable but unverified against a real account. The
matrix is re-verified at each release; rows never claim support that was not
exercised against a real account. Details:
[docs/providers.md](docs/providers.md).

## Deploying

Everything deploys into your Modal workspace. The deployment creates:

| Resource | Name | Purpose |
| --- | --- | --- |
| Modal App | `sbx-control` (`SBX_MODAL_APP_NAME` to rename) | `/v1` + `/api` ASGI app, reaper cron |
| Named Images | `sbx-runtime` (+ `-devin` / `-antigravity` / `-grok` / `-opencode`) | per-provider sandbox images |
| Dicts | `sbx-sessions`, `sbx-runs`, `sbx-accounts`, `sbx-workflows` | durable state |
| Secrets | `sbx-codex-auth`, `sbx-basic-auth`, `sbx-v1-bootstrap`, `sbx-acct-<id>` | credentials — never committed |

```bash
sbx deploy         # idempotent: builds images, seeds Dicts/Secrets, deploys
sbx upgrade        # re-deploy keeping durable runs/accounts/artifacts
sbx uninstall      # stop app + sandboxes; durable data stays unless
                   # --purge-data / --purge-credentials is passed
```

Manual equivalent (`modal secret create`, `make image*`, `make deploy`),
custom app names, the optional Cloudflare Worker edge (`deploy/sbx-edge`),
and uninstall verification are in
[docs/deployment.md](docs/deployment.md).

## Credentials

Provider credentials are imported as file blobs — the same files the official
CLIs write on `login`:

| Provider | Login locally | File imported |
| --- | --- | --- |
| codex | `codex login` | `~/.codex/auth.json` |
| devin | `devin` (interactive login) | `~/.local/share/devin/credentials.toml` |
| antigravity | `agy` (OAuth login) | `~/.gemini/antigravity-cli/antigravity-oauth-token` |
| grok | `grok` login | `~/.grok/auth.json` |
| opencode | `opencode` (login writes `auth.json`) | `~/.local/share/opencode/auth.json` |

```bash
python -m control.onboarding --modal import --provider <p> --from <path-or-home>
# or: POST /v1/accounts {provider, label, credential:{files:{...}}} (admin key)
```

Rules enforced everywhere: blobs travel only over HTTPS into your workspace;
files are restored at `0600` inside the sandbox; the runner strips the blob
env var from the CLI child process; `runner export-credentials` writes
refreshed credentials back to the account Secret; **never paste a token into
an issue, log, PR, or fixture** (`REDACTED` placeholders only). See
[SECURITY.md](SECURITY.md) and [docs/providers.md](docs/providers.md).

## API / SDK examples

`examples/sbx_client.py` is a dependency-free (`httpx` only) reference client
for the whole surface:

```python
from examples.sbx_client import SbxClient

client = SbxClient()  # SBX_BASE_URL + SBX_API_KEY

created = client.create("Add a /health endpoint", provider="codex")
agent, run = created["agent"], created["run"]

for ev in client.watch(agent["id"], run["id"]):  # SSE with Last-Event-ID resume
    print(ev.type)

final = client.wait(agent["id"], run["id"])  # persisted terminal status
follow = client.followup(agent["id"], "Now add tests")
client.cancel(agent["id"], follow["id"])

results = client.wait_many([(agent["id"], run["id"]), (agent2, run2)])

recovery = client.recover("wf-123")  # re-attach after process restart
patch = client.artifacts.download(agent_id, artifact_id, dest="patch.diff")
client.close_workflow("wf-123")  # scoped cleanup
```

The full OpenAPI contract is [docs/contracts/api-v1.yaml](docs/contracts/api-v1.yaml);
the Cursor Cloud Agents field mapping is in [examples/README.md](examples/README.md).

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `sbx deploy` can't reach Modal | Not authenticated: `modal token new`, or wrong `MODAL_PROFILE`. `sbx doctor` reports presence/status, never values. |
| `401 unauthorized` on `/v1` | Missing/wrong `SBX_API_KEY`, or key revoked (`DELETE /v1/api-keys/{id}` earlier). Verify with `GET /v1/me`. |
| Run `ERROR` with `error.code=auth_invalid` | Provider credential expired/invalid. Re-import (`python -m control.onboarding --modal import`) or probe it: `POST /v1/accounts/{id}/verify`. |
| `429 provider_exhausted` / `concurrency_limit` | No free account slot or global cap. Honor `error.retry_after`, add accounts (`SBX_<PROVIDER>_ACCOUNTS`) or raise `max_concurrent`. |
| Agent stuck `creating` then `lost` | Sandbox create failed (image missing, Secret missing). Re-run `make image*` / `sbx deploy`, then `sbx doctor`. |
| Agent `timed_out` / `lost` | Idle timeout or reaper sweep — expected lifecycle. History stays read-only; create a new agent. |
| Event stream stalls | `watch` reconnects with `Last-Event-ID` (bounded); after it ends, `wait`/`get_run` is the durable fallback — never retry forever. |
| Leftover sandboxes | `DELETE /v1/agents/{id}` or `client.close_workflow(id)`; reaper cron sweeps stale records. Verify `modal sandbox list` is empty. |

`sbx doctor` checks Modal auth, required Secrets, control URL, `/v1` auth,
provider availability and cleanup capability — output is presence/hash-prefix
only, never secret material.

## Known limitations

- **Alpha.** `/v1` responses may change before 1.0; contracts live in
  `docs/contracts/` and are versioned with the release.
- **Self-hosted only.** No hosted SaaS, no multi-tenant control plane, no
  billing. `cost_estimate_usd` is a Modal list-price *estimate*, not real
  billing.
- **No browser layer.** The noVNC/browser-execution tier is out of scope
  for 0.1.
- **Provider coverage.** Only codex is Stable in this tag; devin /
  antigravity / grok / opencode are Experimental — all four passed
  real-account Modal gates on the RC plane, while codex's own RC lane is
  credential-deferred on a stale ChatGPT token (external, not a defect);
  claude is **not** supported (see the matrix —
  nothing is claimed without real-account evidence).
- **Single workspace.** One deployment = one Modal workspace; API keys are
  deployment-scoped (`sbx_<key>`, stored as `sha256` only).
- **Lifecycle caps.** 30 min idle reclaim (configurable), 4 h hard sandbox
  cap, 15 min per-turn soft cap.
- **Sandbox-local files are ephemeral.** `events.jsonl`, `inbox/`, `turns/`
  live in the sandbox; durable outcomes are the run ledger and artifacts —
  export artifacts before closing an agent if you need the patch.

## Development

```bash
make lint      # ruff check + format
make test      # unit + integration — no cloud credentials needed
make test-e2e  # Playwright against the local mock
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev loop, the provider adapter
contract, and the rules for real-credential tests.

## License & security

License selection is **pending Owner decision** — see [LICENSE](LICENSE)
(it is an explicit placeholder, not a grant). Report vulnerabilities
privately per [SECURITY.md](SECURITY.md); never file credentials, tokens, or
credential blobs in issues or PRs.
