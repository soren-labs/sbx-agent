.PHONY: lint test test-e2e image image-devin image-antigravity image-grok deploy secrets test-e2e-modal

export MODAL_PROFILE ?= sorenlab2026

# spike/ is owned by the P0 orchestrator (SOR-28) and currently fails ruff (E501/F401);
# tracked as SOR-43 (child of SOR-29). Wave 1 lint excludes it; do not edit spike/.
lint:
	uv run ruff check . --exclude spike
	uv run ruff format --check . --exclude spike

# Unit + integration. Must not talk to Modal (no `make image`, no credentials).
test:
	uv run pytest tests/unit tests/integration

test-e2e:
	npm --prefix web ci
	cd web && NODE_PATH="$(CURDIR)/web/node_modules" npx playwright test

# Named Image sbx-runtime (Modal credentials required). Equivalent: `python -m runtime.image`.
image:
	uv run python -m runtime.image

# SOR-74 Devin fast path: named Image sbx-runtime-devin (sbx-runtime + pinned
# standalone Devin CLI). Same Modal credentials requirement as `image`.
image-devin:
	uv run python -m runtime.image --devin

# SOR-62/SOR-80 provider fast path: named Images carrying the host CLI binary
# (never committed). SBX_AGY_BIN / SBX_GROK_BIN override ~/.local/bin defaults.
image-antigravity:
	uv run python -m runtime.image --provider antigravity

image-grok:
	uv run python -m runtime.image --provider grok

# WP1-C owns the real control-plane deploy. Sandbox params stay out of the image.
deploy:
	uv run python -c "from runtime.image import invoke_control_deploy; invoke_control_deploy()"

# Write Cloud Secrets into ~/.modal.toml and ~/.codex/auth.json. Never echo values.
secrets:
	uv run python -c "from runtime.image import write_local_secrets; write_local_secrets()"

# Real Modal e2e is WP2-H (SOR-42). This target only runs it when that tree exists.
test-e2e-modal:
	@if [ -d tests/e2e_modal ]; then \
		uv run pytest tests/e2e_modal; \
	else \
		echo "tests/e2e_modal is owned by WP2-H (SOR-42); not implemented in WP1-A."; \
	fi
