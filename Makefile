.PHONY: lint test test-e2e spec-check console-build docs-check
lint:
	uv run ruff check .
	uv run ruff format --check .
spec-check:
	uv run python scripts/unified_spec.py --check
test:
	uv run pytest tests/unit tests/integration -n 4
test-e2e:
	npm --prefix console test
console-build:
	npm --prefix console run typecheck
	npm --prefix console run build
docs-check:
	npm --prefix docs-site run check
