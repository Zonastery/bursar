# bursar root Makefile.
#
# Portability (L8): the `test-js-integration` recipe is multi-line and relies on
# GNU make's `.ONESHELL:`, which is IGNORED by GNU make < 3.82 (notably the
# make 3.81 that ships with macOS). Install a modern GNU make (`brew install
# make`, then use `gmake`) or run the recipe under bash. We enforce the minimum
# below so the failure is loud rather than silent.
ifeq ($(filter oneshell,$(.FEATURES)),)
$(error This Makefile needs GNU make >= 3.82 for .ONESHELL. On macOS: 'brew install make' then run 'gmake'.)
endif

.ONESHELL:
SHELL := /bin/bash

.PHONY: help test test-python test-js test-pg-up test-pg-down test-integration
.DEFAULT_GOAL := help

TEST_PG_NAME ?= bursar-test-pg
TEST_PG_PORT ?= 55432
TEST_PG_DATABASE ?= bursar
TEST_PG_USER ?= postgres
TEST_PG_PASSWORD ?= bursar
TEST_PG_URL ?= postgresql://$(TEST_PG_USER):$(TEST_PG_PASSWORD)@localhost:$(TEST_PG_PORT)/$(TEST_PG_DATABASE)

help:                                ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install-hooks:                       ## Install lefthook git hooks (requires npm or uv)
	@if command -v npx &>/dev/null && npx --yes lefthook install 2>/dev/null; then \
		echo "hooks installed via npx"; \
	elif command -v uvx &>/dev/null && uvx lefthook install 2>/dev/null; then \
		echo "hooks installed via uvx"; \
	else \
		echo "Install lefthook via npm install -g lefthook or brew install lefthook" >&2; \
		exit 1; \
	fi

test: test-integration               ## All tests (Python + JS, incl. real-Postgres integration)

test-pg-up:                         ## Start an isolated Postgres database for integration tests
	docker rm -f $(TEST_PG_NAME) >/dev/null 2>&1 || true
	docker run -d --name $(TEST_PG_NAME) \
	  -e POSTGRES_USER=$(TEST_PG_USER) \
	  -e POSTGRES_PASSWORD=$(TEST_PG_PASSWORD) \
	  -e POSTGRES_DB=$(TEST_PG_DATABASE) \
	  -p $(TEST_PG_PORT):5432 \
	  postgres:16 -c max_connections=500
	for i in $$(seq 1 30); do
	  if docker exec $(TEST_PG_NAME) pg_isready -U $(TEST_PG_USER) -d $(TEST_PG_DATABASE) >/dev/null 2>&1; then exit 0; fi
	  sleep 1
	done
	echo "Postgres did not become ready" >&2
	exit 1

test-pg-down:                       ## Stop and remove the isolated Postgres test database
	docker rm -f $(TEST_PG_NAME) >/dev/null 2>&1 || true

test-integration:                  ## Run Python and JS tests against an isolated Postgres database
	$(MAKE) test-pg-up
	trap '$(MAKE) test-pg-down' EXIT
	DATABASE_URL=$(TEST_PG_URL) $(MAKE) test-python
	DATABASE_URL=$(TEST_PG_URL) $(MAKE) test-js

# Both suites resolve a real Postgres via DATABASE_URL (CI's service
# container / an already-running instance) or, failing that, via
# testcontainers — a disposable postgres:16 spun up automatically for the
# duration of the run (Docker permitting). No manual container orchestration
# needed; see python/tests/conftest.py and javascript/tests/global-setup.ts.
test-python:                         ## Python tests (mock + postgres via DATABASE_URL/testcontainers)
	cd python && pytest

test-js:                             ## JS tests (mock + postgres via DATABASE_URL/testcontainers)
	cd javascript && npx vitest run
