# sbx-browser

SBX runs coding agents as durable **Sessions**. Each Session owns a logical
Worktree; every Turn runs the provider's official CLI (OpenCode, Codex) inside
`sbx-runtime` on a local or Modal Executor. Finished work is captured as an
immutable **ChangeSet**, shipped by an exact-subject **Delivery** (GitHub pull
request and gated merge), and can be reviewed by child-Session **Delegations**.
PostgreSQL is the only business authority; there is one API (`/api`), one
Console and one SDK/CLI.

The architecture is specified in [`docs/architecture/unified/`](docs/architecture/unified/README.md);
what this implementation actually does is in [`docs/specs/unified/`](docs/specs/unified/README.md).

## Run a control plane

```bash
uv sync
export SBX_DATABASE_URL=postgresql://...          # PostgreSQL 15+
export SBX_VAULT_KEYS="k1:$(openssl rand -base64 32)"
export SBX_RUNTIME_MASTER_KEY="$(openssl rand -hex 32)"
export SBX_PUBLIC_URL=http://localhost:5174 SBX_ALLOWED_ORIGINS=http://localhost:5174
make serve                                        # /api + Job workers on :8800
make console-dev                                  # Console on :5174, proxies /api to :8800
```

Sign up with email and password, then add Connections in **Settings →
Connections** (or `sbx connections add`): a Modal token for compute, an
OpenCode Zen key (or Codex `auth.json`) for the agent, and a GitHub token for
repositories and pull requests. Secrets are stored encrypted as
CredentialVersions and are never returned by the API.

## SDK and CLI

```python
from sbx import SBXClient

client = SBXClient("http://127.0.0.1:8800", api_key)
result = client.execute(
    "Add a CONTRIBUTING.md with one paragraph.",
    repository={"full_name": "owner/repo"},
    executor={"backend": "modal"},
)
changeset = client.changesets.wait_ready(result["session_id"], source_turn_id=result["turn"]["id"])
delivery = client.deliveries.wait(client.deliveries.request(changeset["id"])["id"])
```

```bash
sbx auth login --email you@example.com          # password from stdin or prompt
sbx connections add opencode_zen < key.txt
sbx execute "Fix the flaky test" --project my-project
```

See [`examples/unified_mvp.py`](examples/unified_mvp.py) for the full
create → ChangeSet → review → Delivery walkthrough.

## Development

```bash
make lint            # ruff check + format check
make test            # unit + integration; embedded PostgreSQL, no cloud credentials
make console-check   # Console typecheck, tests and build
make docs-check      # documentation site build and link checks
make openapi         # regenerate docs/specs/unified/openapi.yaml
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and
[SECURITY.md](SECURITY.md) for vulnerability reporting. Pre-unification
contracts and design notes are kept read-only under [`docs/archive/`](docs/archive/).

License selection is pending owner decision; [LICENSE](LICENSE) is currently
a placeholder, not a grant.
