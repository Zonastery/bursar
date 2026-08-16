.PHONY: build clean lint typecheck test test-concurrency release-ci help
.DEFAULT_GOAL := help

# Single source of truth for the version: pyproject.toml (H9). Read it directly
# so the release tag never drifts from what gets published — no hardcoded or
# __version__-derived value that can disagree with the package metadata.
VERSION := $(shell uv run python -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

build: clean  ## Build the wheel + sdist
	uv build

clean:  ## Remove build artifacts and caches
	rm -rf dist/ build/ *.egg-info/
	rm -rf .pytest_cache .ruff_cache __pycache__/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

lint:  ## Run ruff lint
	uv run ruff check src/ tests/ scripts/

typecheck:  ## Run pyright (including warnings)
	uv run pyright src/ --warnings

test: lint typecheck  ## Lint, typecheck, then run the test suite
	uv run python -m pytest tests/ -q

test-concurrency:  ## Run the real-Postgres concurrency subset (set DATABASE_URL)
	uv run python -m pytest tests/ -m concurrency -q --count $${BURSAR_RACE_ITERATIONS:-1}

release-ci:  ## Print the git tag commands for a CI-driven release
	@echo "Release via CI: git tag v$(VERSION) && git push origin v$(VERSION)"
