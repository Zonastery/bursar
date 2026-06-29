# ducto root Makefile.
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

PG_IMG ?= postgres:16
PG_PORT ?= 5434
PG_DB  ?= ducto_test
PG_USER?= ducto
# Test-only password for an ephemeral local/CI Postgres container. Override via
# the environment for anything non-disposable; it is passed to Docker and the
# test runner through the environment, never interpolated onto a command line.
PG_PASS?= ducto
PG_NAME?= ducto-pg-js
# Exported so child recipes inherit it from the environment (C8) instead of it
# appearing as a CLI argument (where it would leak via `ps`/history/CI logs).
export DATABASE_URL ?= postgres://$(PG_USER):$(PG_PASS)@localhost:$(PG_PORT)/$(PG_DB)

.PHONY: help test test-python test-js test-js-mock test-js-integration clean-db
.DEFAULT_GOAL := help

help:                               ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

test: test-python test-js           ## All tests (Python + JS mock + JS integration)

test-python:                        ## Python tests (mock + postgres via pg_tmp)
	cd python && pytest

test-js: test-js-mock test-js-integration  ## All JS tests

test-js-mock:                       ## JS mock tests (no infra needed)
	cd javascript && npx vitest run

test-js-integration:                ## JS postgres integration tests (docker)
	docker rm -f $(PG_NAME) 2>/dev/null; true
	docker run -d --name $(PG_NAME) \
		-e POSTGRES_DB=$(PG_DB) \
		-e POSTGRES_USER=$(PG_USER) \
		-e POSTGRES_PASSWORD=$(PG_PASS) \
		-p $(PG_PORT):5432 \
		$(PG_IMG)
	until docker exec $(PG_NAME) pg_isready -U $(PG_USER) 2>/dev/null; do sleep 1; done
	# DATABASE_URL is exported above, so vitest inherits it from the environment.
	cd javascript && npx vitest run tests/store-integration.test.ts; \
		rc=$$?; \
		docker stop $(PG_NAME) >/dev/null && docker rm $(PG_NAME) >/dev/null; \
		exit $$rc

clean-db:                           ## Cleanup leftover test pg container
	docker rm -f $(PG_NAME) 2>/dev/null; true
