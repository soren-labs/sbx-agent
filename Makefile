.PHONY: lint test test-e2e console-dev docs-dev docs-build docs-check docs-deploy docs-screenshots image image-devin image-antigravity image-grok image-opencode image-manifest deploy secrets test-e2e-modal

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

# Web console against a real, cloud-free control plane (SBX_BACKEND=local,
# fake provider CLIs, throwaway state). Prints the URL and a dev API key.
console-dev:
	uv run python tests/e2e/serve_console.py --port $${PORT:-8790}

# Documentation website (Astro Starlight) in docs-site/.
docs-dev:
	npm --prefix docs-site ci
	npm --prefix docs-site run dev

docs-build:
	npm --prefix docs-site ci
	npm --prefix docs-site run build

# Build the site and run the docs link/structure check.
docs-check:
	npm --prefix docs-site ci
	npm --prefix docs-site run check

# Regenerate the errors reference table from control/api_v1/error_catalog.py.
docs-sync-errors:
	uv run python docs-site/scripts/sync_error_reference.py

# Refresh the console guide's screenshots from the Playwright console suite.
docs-screenshots: test-e2e
	node docs-site/scripts/sync-console-screenshots.mjs

# Deploy the built docs site as the `sbx-docs` Modal app (Modal credentials
# required). Prints the *.modal.run URL; mapping a custom domain is a DNS /
# workspace step documented in docs-site/src/content/docs/self-hosting/deploy.md.
docs-deploy: docs-build
	uv run modal deploy docs-site/modal_deploy.py

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

# Release 0.1 OpenCode seam (SOR-96): named Image sbx-runtime-opencode —
# sbx-runtime + pinned opencode-ai npm package (packages.txt opencode_*).
# Reproducible — no host artifact needed; same Modal credentials as `image`.
image-opencode:
	uv run python -m runtime.image --provider opencode

# Doctor / release-evidence input: provider → image/CLI/pin manifest as JSON.
# No Modal credentials required.
image-manifest:
	uv run python -m runtime.image --manifest


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

# Hosted Alpha: credential-free local UI/provider fakes and full acceptance.
.PHONY: hosted-mock test-hosted-alpha
hosted-mock:
	VITE_HOSTED=1 npm --prefix console run build
	uv run python deploy/hosted/mock_server.py

test-hosted-alpha:
	VITE_HOSTED=1 npm --prefix console run build
	uv run --with playwright pytest tests/e2e/test_hosted_alpha_gate.py -q
