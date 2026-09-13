.PHONY: lint test test-e2e image deploy

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

# WP1-C owns the real control-plane deploy. Sandbox params stay out of the image.
deploy:
	uv run python -c "from runtime.image import invoke_control_deploy; invoke_control_deploy()"
