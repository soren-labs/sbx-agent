---
title: Contributing
description: Development environment, architecture rules and the pull request checklist.
---

## Development environment

```bash
uv sync              # Python >= 3.12
make lint            # ruff check + ruff format --check
make test            # embedded PostgreSQL, local Executor, recorded fixtures
make console-check   # Console typecheck, tests and build
make docs-check      # this site: build, then link checks
make openapi         # regenerate docs/specs/unified/openapi.yaml
```

`make test` must never need cloud credentials.

## Architecture rules

- `docs/architecture/unified/` is the normative RFC; `docs/specs/unified/`
  describes implemented behaviour and is updated with it.
- PostgreSQL is the only business authority and every resource has one writer.
- `tests/unit/test_layer_boundaries.py` enforces import direction, and
  `tests/unit/test_openapi_drift.py` fails when the OpenAPI document drifts.
- Tests run against a real throwaway PostgreSQL cluster, never a mock.

## Secrets

Fixtures use the literal `REDACTED` for token, password and secret fields. Opt-in
live scripts read credentials from the environment, never print them and must
terminate every sandbox they create.

## Pull requests

- `make lint`, `make test` and `make console-check` pass without credentials.
- No credentials or real account data in the diff.
- Specs and docs are updated when behaviour, variables, commands or routes
  change.
