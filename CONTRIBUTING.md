# Contributing

## Dev environment

```bash
uv sync              # Python >= 3.12, dev deps in [dependency-groups]
make lint            # ruff check + ruff format --check (spike/ is excluded)
make test            # pytest tests/unit tests/integration — embedded PostgreSQL,
                     # local Executor, recorded fixtures; no cloud credentials
make console-check   # Console typecheck, vitest, build (Node 22)
make docs-check      # docs-site build + link checks
```

## Architecture rules

- The RFC in `docs/architecture/unified/` is normative; `docs/specs/unified/` documents the
  implemented behaviour and must be updated with it.
- PostgreSQL is the only business authority. Each resource has one writer
  (`docs/specs/unified/authority.md`); side effects run as Jobs with claims and fences.
- `tests/unit/test_layer_boundaries.py` enforces import direction
  (`domain` ← `application` ← `api`/`jobs`/`integrations`/`executors`; `runtime` and
  `protocol` never import `control`).
- The OpenAPI document is generated (`make openapi`); `tests/unit/test_openapi_drift.py` fails
  on drift.
- `import modal` is confined to `control/executors/modal.py` and
  `control/integrations/connectors/modal.py`; tests never reach a real Modal client.

## Harnesses

Each provider CLI implements the `Harness` Protocol in `runtime/harnesses/protocol.py`
(`describe`, `prepare`, `start_turn`/`resume_turn`, `normalize`/`finish`, `classify_outcome`,
`native_state_paths`, `release`). Manifests are pinned in
`docs/specs/unified/harnesses/manifests.json`. Recorded native streams live in
`tests/fixtures/harnesses/<provider>/`; the fake official CLI is in `tests/fakes/official/`.
Normalizers must never invent usage, and a stale resume id must fail the Turn rather than fork.

## Test isolation and secrets

- `tests/conftest.py` strips host credentials (including `SBX_TEST_*` and `SBX_BENCHMARK_*`)
  and isolates `HOME`/XDG. Tests pass every needed variable explicitly.
- Token/password/secret fields in fixtures are always the literal `REDACTED`.
- Opt-in live scripts (`make smoke-modal`, `make check-connectors`, `make mvp-acceptance`) read
  credentials from the environment, never print them, and must terminate every sandbox they
  create. `make mvp-acceptance` writes redacted evidence and scans API responses, logs, the
  database and the pull request for credential values.

## PR checklist

- `make lint`, `make test` and `make console-check` green without cloud credentials.
- No credentials, tokens or real account data anywhere in the diff.
- Specs/docs updated when behaviour, env vars, commands or routes change.
