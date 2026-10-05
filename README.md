# sbx-browser

Self-hosted orchestration for coding-agent CLIs, built on the unified
architecture defined by RFC 167 (`docs/architecture/unified/`).

You create a **Session** — a durable identity bound to a workspace —
against a project or ad-hoc repository. Turns dispatch through durable
Jobs to a replaceable **ExecutorLease** (a Modal sandbox running the
`sbx-runtime` daemon), where a thin **Harness** drives the provider's
official CLI (OpenCode Zen). Mutable worktree state is captured as an
immutable **ChangeSet**; **Delivery** performs exact-subject git effects
(push/branch/draft PR); **Delegation** spawns child Sessions (review,
test, research) whose results pin to the exact ChangeSet digest.

All state lives in typed relational projections plus the append-only
`session_events` journal; secrets live only in encrypted
`CredentialVersion` records. One business API under `/api`, one typed
Console, one Python SDK (`sbx.sdk.unified.UnifiedClient`).

## Credential classes (MVP)

- SBX email + password (product auth)
- OpenCode Zen API key (`opencode_zen` connection)
- Modal Token ID + Token Secret (`modal` connection)
- GitHub personal token (`github` connection)

## Development

```bash
make lint          # ruff check + format
make test          # unit + Postgres integration (no cloud credentials)
make image         # build/publish the named Modal image (Modal creds)
make test-mvp      # real end-to-end acceptance (real creds, disposable DB)
make console-dev   # serve the unified API locally (SBX_TEST_DATABASE_URL)
```

See [AGENTS.md](AGENTS.md) for the architecture invariants and rules.
[LICENSE](LICENSE) is currently a placeholder, not a grant.
