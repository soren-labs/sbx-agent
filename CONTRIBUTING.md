# Contributing

Thanks for helping. A few project-specific rules matter more than style —
read them before your first PR. The architecture invariants live in
[AGENTS.md](AGENTS.md) and `docs/architecture/unified/` (RFC 167, normative).

## Dev environment

```bash
uv sync            # python >=3.12, hatchling build, dev deps in [dependency-groups]
make lint          # ruff check + ruff format --check
make test          # pytest tests/unit tests/integration — MUST pass with no
                   # cloud credentials and no Modal connection
make console-dev   # serve the unified API locally (SBX_TEST_DATABASE_URL)
```

`make test` must never open a real Modal connection — `modal` is only
imported in `control/executors/modal.py` and `runtime/image.py`, and tests
must not trigger those paths.

## Test isolation (hard rules)

- `tests/conftest.py` strips host credentials and isolates `HOME`/XDG. Tests
  must not read, print, or inherit real credentials.
- Postgres integration tests (`tests/integration/postgres/`) require a
  disposable database via `SBX_TEST_DATABASE_URL` and skip without it —
  never point it at production.
- Fixture/stub token fields are always `REDACTED`.
- Real-network acceptance (`make test-mvp`, `tests/mvp/`) uses real
  credentials from the environment and only writes to the disposable
  `soren-labs/sbx-e2e-test` repository.

## Changes to the runtime image

Pins live in `runtime/packages.txt`; `python -m runtime.image` regenerates
`Dockerfile.local` / `Dockerfile.opencode.local`, resolves `latest`
requests, and builds/publishes named Modal images. Republish
`sbx-runtime-opencode` after changing `runtime/daemon` or `protocol/`.

See [SECURITY.md](SECURITY.md) for vulnerability reporting.
