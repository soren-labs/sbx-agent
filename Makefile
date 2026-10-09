.PHONY: lint test console-check console-dev serve worker migrate openapi docs-dev docs-build docs-check smoke-modal check-connectors mvp-acceptance

lint:
	uv run ruff check . --exclude spike
	uv run ruff format --check . --exclude spike

# Unit + integration (embedded PostgreSQL, local Executor, recorded Harness
# fixtures). Must never need cloud credentials.
test:
	uv run pytest tests/unit tests/integration

console-check:
	npm --prefix console ci
	npm --prefix console run typecheck
	npm --prefix console test
	npm --prefix console run build

# Vite dev server on :5174; proxies /api to SBX_API_PROXY_TARGET (default :8800).
console-dev:
	npm --prefix console run dev

# Control plane: /api + Job workers in one process. Requires SBX_DATABASE_URL,
# SBX_VAULT_KEYS and SBX_RUNTIME_MASTER_KEY.
serve:
	uv run python -m control.composition serve --port $${PORT:-8800}

worker:
	uv run python -m control.composition worker

migrate:
	uv run python -m control.composition migrate

# Regenerate docs/specs/unified/openapi.yaml (drift: tests/unit/test_openapi_drift.py).
openapi:
	uv run python scripts/export_openapi.py

docs-dev:
	npm --prefix docs-site ci
	npm --prefix docs-site run dev

docs-build:
	npm --prefix docs-site ci
	npm --prefix docs-site run build

docs-check:
	npm --prefix docs-site ci
	npm --prefix docs-site run check

# Opt-in live checks: credentials come from the environment and are never printed.
smoke-modal:
	uv run python tests/e2e_modal/smoke_executor.py

check-connectors:
	uv run python tests/e2e_modal/live_connectors.py

# Real MVP acceptance: email/password + Modal + GitHub token + a BYOK inference key
# (SBX_TEST_INFERENCE_API_KEY; DeepSeek endpoints by default). Creates
# Modal sandboxes and a pull request in SBX_BENCHMARK_GITHUB_REPO (must be */sbx-e2e-test),
# then always terminates its sandboxes and deletes the refs it created.
mvp-acceptance:
	uv run python tests/e2e_modal/mvp_acceptance.py
