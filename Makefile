# bursar root Makefile.
#
# The `test-integration` recipe is multi-line and relies on
# GNU make's `.ONESHELL:`, which is IGNORED by GNU make < 3.82 (notably the
# make 3.81 that ships with macOS). Install a modern GNU make (`brew install
# make`, then use `gmake`) or run the recipe under bash. We enforce the minimum
# below so the failure is loud rather than silent.
ifeq ($(filter oneshell,$(.FEATURES)),)
$(error This Makefile needs GNU make >= 3.82 for .ONESHELL. On macOS: 'brew install make' then run 'gmake'.)
endif

.ONESHELL:
SHELL := /bin/bash

.PHONY: help test test-python test-js test-go test-go-coverage test-go-adk-coverage go-format go-vet go-staticcheck test-pg-build test-pg-up test-pg-down test-integration
.DEFAULT_GOAL := help

GO_TEST_COVERAGE := github.com/vladopajic/go-test-coverage/v2@v2.19.0

TEST_PG_NAME ?= bursar-test-pg
TEST_PG_IMAGE ?= bursar/postgres-test:17.10-pg-jsonschema-0.3.4
TEST_PG_BUILD_CONTEXT ?= tests/postgres
TEST_PG_PORT ?= 55432
TEST_PG_DATABASE ?= bursar
TEST_PG_USER ?= postgres
TEST_PG_PASSWORD ?= bursar
TEST_PG_URL ?= postgresql://$(TEST_PG_USER):$(TEST_PG_PASSWORD)@localhost:$(TEST_PG_PORT)/$(TEST_PG_DATABASE)
TEST_PRIMARY_TENANT_ID ?= 00000000-0000-4000-8000-000000000201
TEST_SECONDARY_TENANT_ID ?= 00000000-0000-4000-8000-000000000202
TEST_TENANT_CONFIG ?= .github/package-smoke/pricing.json

help:                                ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install-hooks:                       ## Install lefthook git hooks (requires Bun or uv)
	@if command -v bunx &>/dev/null && (cd javascript && bunx --no-install lefthook install) 2>/dev/null; then \
		echo "hooks installed via bunx"; \
	elif command -v uvx &>/dev/null && uvx lefthook install 2>/dev/null; then \
		echo "hooks installed via uvx"; \
	else \
		echo "Run 'cd javascript && bun ci' first, or install lefthook with brew" >&2; \
		exit 1; \
	fi

test: test-integration               ## All tests (Python + JS + Go, incl. real-Postgres integration)

test-pg-build:                      ## Build PostgreSQL with Bursar's required extensions
	docker build -t $(TEST_PG_IMAGE) $(TEST_PG_BUILD_CONTEXT)

test-pg-up: test-pg-build           ## Start an isolated Postgres database for integration tests
	docker rm -f $(TEST_PG_NAME) >/dev/null 2>&1 || true
	docker run -d --name $(TEST_PG_NAME) \
	  -e POSTGRES_USER=$(TEST_PG_USER) \
	  -e POSTGRES_PASSWORD=$(TEST_PG_PASSWORD) \
	  -e POSTGRES_DB=$(TEST_PG_DATABASE) \
	  -p $(TEST_PG_PORT):5432 \
	  $(TEST_PG_IMAGE) -c max_connections=500
	for i in $$(seq 1 30); do
	  if docker exec $(TEST_PG_NAME) pg_isready -U $(TEST_PG_USER) -d $(TEST_PG_DATABASE) >/dev/null 2>&1; then exit 0; fi
	  sleep 1
	done
	echo "Postgres did not become ready" >&2
	exit 1

test-pg-down:                       ## Stop and remove the isolated Postgres test database
	docker rm -f $(TEST_PG_NAME) >/dev/null 2>&1 || true

test-integration:                  ## Run Python, JS, and Go tests against an isolated Postgres database
	set -euo pipefail
	$(MAKE) test-pg-up
	trap '$(MAKE) -C "$(CURDIR)" test-pg-down' EXIT
	BURSAR_ALLOW_DATABASE_RESET=1 DATABASE_URL=$(TEST_PG_URL) $(MAKE) test-python
	BURSAR_ALLOW_DATABASE_RESET=1 DATABASE_URL=$(TEST_PG_URL) $(MAKE) test-js
	# Python and JavaScript reset fixtures leave no tenant behind. Bootstrap the
	# fixed pair used by the Go suite after those resets, including its catalog.
	cd python
	DATABASE_URL=$(TEST_PG_URL) BURSAR_OPERATOR_DATABASE_URL=$(TEST_PG_URL) BURSAR_PROVIDER_ENVIRONMENT=test \
	  uv run bursar tenant bootstrap package-smoke "../$(TEST_TENANT_CONFIG)" \
	  --id "$(TEST_PRIMARY_TENANT_ID)" --display-name "Package smoke" --label initial
	DATABASE_URL=$(TEST_PG_URL) BURSAR_OPERATOR_DATABASE_URL=$(TEST_PG_URL) BURSAR_PROVIDER_ENVIRONMENT=test \
	  uv run bursar tenant bootstrap package-smoke-secondary "../$(TEST_TENANT_CONFIG)" \
	  --id "$(TEST_SECONDARY_TENANT_ID)" --display-name "Package smoke secondary" --label initial
	cd ..
	BURSAR_ALLOW_DATABASE_RESET=1 DATABASE_URL=$(TEST_PG_URL) \
	BURSAR_TENANT_ID=$(TEST_PRIMARY_TENANT_ID) \
	BURSAR_SECONDARY_TENANT_ID=$(TEST_SECONDARY_TENANT_ID) \
	BURSAR_PROVIDER_ENVIRONMENT=test BURSAR_REQUIRE_POSTGRES_TESTS=1 $(MAKE) test-go-coverage
	$(MAKE) test-go-adk-coverage

# Python and JavaScript suites resolve a real Postgres via DATABASE_URL (CI's service
# container / an already-running instance) or, failing that, via
# testcontainers — disposable PostgreSQL 17 with pg_partman 5 and
# pg_jsonschema 0.3, spun up
# automatically for the duration of the run (Docker permitting). No manual
# container orchestration needed; see python/tests/conftest.py and
# javascript/tests/global-setup.ts.
test-python:                         ## Python tests (mock + postgres via DATABASE_URL/testcontainers)
	cd python && uv run pytest

test-js:                             ## JS tests (mock + postgres via DATABASE_URL/testcontainers)
	cd javascript && bun run test

test-go:                             ## Go tests (mock; Postgres only when DATABASE_URL is supplied)
	cd golang && go test -race ./...

test-go-coverage:                    ## Go SDK race tests and 90% package/total coverage gate (supply DATABASE_URL)
	cd golang
	mkdir -p coverage
	go test -race -count=1 -covermode=atomic -coverpkg=./... -coverprofile=coverage/core.out ./...
	go run $(GO_TEST_COVERAGE) --config=.testcoverage.yml

test-go-adk-coverage:                ## Google ADK integration race tests and 90% coverage gate
	cd golang/integrations/googleadk
	mkdir -p coverage
	go test -race -count=1 -covermode=atomic -coverpkg=./... -coverprofile=coverage/adk.out ./...
	go run $(GO_TEST_COVERAGE) --config=.testcoverage.yml

go-format:                           ## Verify Go source is gofmt-formatted
	files="$$(git ls-files '*.go')"
	if [ -n "$$files" ]; then
	  test -z "$$(gofmt -l $$files)"
	fi

go-vet:                              ## Run Go's standard static analysis
	cd golang && go vet ./...

go-staticcheck:                      ## Run the pinned Staticcheck analysis
	cd golang && go run honnef.co/go/tools/cmd/staticcheck@v0.7.0 ./...
