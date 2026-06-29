.PHONY: build clean lint typecheck test publish publish-test release release-ci help
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

typecheck:  ## Run pyright
	uv run pyright src/

test: lint typecheck  ## Lint, typecheck, then run the test suite
	uv run python -m pytest tests/ -q

publish-test: test build  ## Publish to TestPyPI
	. ./.env && uv publish --publish-url https://test.pypi.org/legacy/

# Manual publish (prefer the CI release for automated publishing):
#   1. git tag v$(VERSION)
#   2. git push origin v$(VERSION)
#   CI runs lint -> typecheck -> test -> build -> publish
publish: build  ## Publish to PyPI
	. ./.env && uv publish

release: publish  ## Publish, then print the released version (tag = pyproject version)
	@echo "Released ducto v$(VERSION)"

release-ci:  ## Print the git tag commands for a CI-driven release
	@echo "Release via CI: git tag v$(VERSION) && git push origin v$(VERSION)"
