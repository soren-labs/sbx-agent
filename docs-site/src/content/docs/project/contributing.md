---
title: Contributing
description: Development workflow and contributor guidelines.
---

## Development setup

```bash
git clone https://github.com/soren-labs/sbx-browser.git
cd sbx-browser
uv sync
```

## Testing

Run the test suites:

```bash
make lint       # ruff check + format --check
make test       # pytest (unit + integration, no cloud)
make test-e2e   # playwright smoke tests (mocked providers)
```

All tests must pass before a PR is accepted. Tests do not require cloud credentials.

### Test isolation

- Tests use isolated `$HOME` and `XDG_*` directories (see `tests/conftest.py`)
- No real Modal or provider credentials are imported
- Fake provider CLIs (`tests/fakes/`) return deterministic outputs
- `SBX_BACKEND=local` uses `LocalProcessBackend` for sandbox execution

**Rules:**
- Do not import the real `modal` client in tests (except in `ModalBackend` skeleton, which tests never touch)
- Do not read or print real credentials
- Credential-shaped env vars are explicitly passed to sandboxes, not inherited from `os.environ`

## Code structure

| Path | Purpose |
| --- | --- |
| `control/` | FastAPI control plane, scheduler, ledger, backends |
| `runtime/` | Sandbox image, entrypoint, runner, provider adapters |
| `web/` | Web console (build-less static SPA for the `/v1` surface) |
| `examples/` | Python client, reference implementations |
| `deploy/` | Optional Cloudflare Worker edge |
| `tests/` | Unit, integration, E2E, and real Modal tests |
| `docs/` | Architecture, deployment, provider guides, frozen contracts |
| `docs-site/` | Astro + Starlight documentation website |

## Contracts (frozen after release)

After release, these files are frozen and require discussion for any changes:

- `docs/contracts/filesystem.md` — `$SBX_WORK` layout
- `docs/contracts/events.md` — canonical event shapes
- `docs/contracts/runner-cli.md` — runner CLI interface
- `docs/contracts/api-v1.yaml` — public REST API
- `control/backend.py` — backend protocol
- `control/ports.py` — scheduler/registry protocols
- `runtime/runner/adapter.py` — provider adapter protocol

## Verification before PR

```bash
# Check style
make lint

# Test all paths
make test

# Manual test on local backend
export SBX_BACKEND=local
uv run sbx deploy
uv run sbx doctor
uv run sbx smoke

# Check contracts
pytest tests/unit/test_contract_consistency.py
```

## Documentation

Write task-oriented docs:

- **Getting started** — prerequisites, step-by-step, first run
- **Guides** — features, design patterns, multi-provider scenarios
- **Reference** — error codes, CLI, event shapes, limits
- **Operations** — deploy, scale, upgrade, troubleshoot

**Style:**
- Lead with the task (\"Create an agent...\"), then steps
- Verify every field name, env var, error code against source
- Use examples (curl, Python, shell)
- Link to related topics
- Avoid internal jargon and ticket references

## Local console development

The web console uses the public `/v1` API with Bearer authentication. One command runs everything locally — `tests/e2e/serve_console.py` boots a real `/v1` control plane on the local backend with fake provider CLIs (no Modal or cloud credentials) and serves `web/` at the same origin:

```bash
make console-dev
```

The console opens at `http://localhost:8790`; the command prints a throwaway API key to paste into the Connect screen.

## Provider adapter contract

To add a new provider, implement the `AgentAdapter` protocol (`runtime/runner/adapter.py`):

```python
@runtime_checkable
class AgentAdapter(Protocol):
    provider: str
    credential_files: tuple[str, ...]  # relative to $HOME, e.g. (".codex/auth.json",)

    def prepare_home(self, home: Path, model: str) -> None:
        \"\"\"Write CLI config / instructions under ``home`` before the first turn.\"\"\"

    def first_turn_argv(self, prompt: str, model: str) -> list[str]:
        \"\"\"argv for turn 1 (no native session id yet).\"\"\"

    def resume_argv(self, prompt: str, native_session_id: str) -> list[str]:
        \"\"\"argv for follow-up turns resuming ``native_session_id``.\"\"\"

    def translate(self, raw_line: str) -> list[dict[str, Any]]:
        \"\"\"Map one native stdout line to 0..n canonical events (events.md).\"\"\"

    def extract_session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        \"\"\"Return the native session id from translated events.\"\"\"

    def health_from(self, exit_code: int | None, stderr_tail: str) -> Health:
        \"\"\"Classify a finished CLI process for account health feedback
        (``ok`` / ``auth_invalid`` / ``rate_limited`` / ``unknown``).\"\"\"
```

Register a factory in the `get_adapter()` registry:

```python
_REGISTRY: dict[str, Callable[[], AgentAdapter]] = {
    "codex": CodexAdapter,
    # "my_provider": MyProviderAdapter,
}
```

## Submitting a PR

1. **Create a branch** off `main`
2. **Test locally** — `make lint && make test`
3. **Open the PR** — describe what changed and link any relevant issues
4. **CI checks** — wait for lint, test, and contract consistency to pass
5. **Review** — address feedback
6. **Merge** — once approved, maintainers merge and close

PR checklist:
- [ ] Code is style-checked (`make lint`)
- [ ] All tests pass (`make test`)
- [ ] Contract tests pass (if touching contracts)
- [ ] No credentials committed
- [ ] Docs updated (if new feature)
- [ ] Issue linked (if applicable)

## Useful commands

```bash
# Run specific test
pytest tests/unit/test_something.py -v

# Check contract consistency
pytest tests/unit/test_contract_consistency.py -v

# Format code
ruff format control/ runtime/ sbx/

# Type check (future)
mypy control/
```

## Questions?

Open a discussion or issue on GitHub.
