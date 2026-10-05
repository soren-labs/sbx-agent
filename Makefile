.PHONY: lint test console-dev image image-manifest test-mvp

export MODAL_PROFILE ?= sorenlab2026

lint:
	uv run ruff check .
	uv run ruff format --check .

# Unified unit + Postgres integration suites. Requires no cloud credentials;
# the Postgres suites skip unless SBX_TEST_DATABASE_URL points at a disposable
# test database.
test:
	uv run pytest tests/unit tests/integration

# Web console against a local unified control plane (uvicorn on :8790).
console-dev:
	uv run python -m tests.mvp.serve_console --port $${PORT:-8790}

# Named runtime Image sbx-runtime-opencode (Modal credentials required).
# Equivalent: `python -m runtime.image --provider opencode`.
image:
	uv run python -m runtime.image --provider opencode

# Provider → image/CLI/pin manifest as JSON (no Modal credentials required).
image-manifest:
	uv run python -m runtime.image --manifest

# Real MVP acceptance: email/password + OpenCode Zen + Modal + GitHub manual
# token end-to-end against a disposable Postgres database. Requires the real
# credentials in env; never prints secret values.
test-mvp:
	uv run python -m tests.mvp.run_acceptance
