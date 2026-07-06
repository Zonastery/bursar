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

.PHONY: help test test-python test-js
.DEFAULT_GOAL := help

help:                                ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

test: test-python test-js            ## All tests (Python + JS, incl. real-Postgres integration)

# Both suites resolve a real Postgres via DATABASE_URL (CI's service
# container / an already-running instance) or, failing that, via
# testcontainers — a disposable postgres:16 spun up automatically for the
# duration of the run (Docker permitting). No manual container orchestration
# needed; see python/tests/conftest.py and javascript/tests/global-setup.ts.
test-python:                         ## Python tests (mock + postgres via DATABASE_URL/testcontainers)
	cd python && pytest

test-js:                             ## JS tests (mock + postgres via DATABASE_URL/testcontainers)
	cd javascript && npx vitest run
