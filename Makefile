.PHONY: lint test test-e2e image deploy

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest tests/unit tests/integration

test-e2e:
	npm --prefix web ci
	cd web && NODE_PATH="$(CURDIR)/web/node_modules" npx playwright test

# Reserved for WP1-A.
image:
	@echo "WP1-A: image target not implemented"

# Reserved for WP1-A.
deploy:
	@echo "WP1-A: deploy target not implemented"
