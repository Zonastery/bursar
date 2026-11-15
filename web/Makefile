# bursar web service Makefile.
#
# Plain GNU make: no .ONESHELL, every recipe is one shell invocation per line
# (or a safe single line), so this also works with the older make that ships
# with macOS.

.PHONY: help tidy-check lint test cover build docker-build ci
.DEFAULT_GOAL := help

VERSION ?= dev
COMMIT ?= unknown
DATE ?= unknown

GO_TEST_COVERAGE := github.com/vladopajic/go-test-coverage/v2@v2.19.0

help:                                ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

tidy-check:                          ## Verify go.mod/go.sum are committed and tidy
	go mod tidy
	git diff --exit-code -- go.mod go.sum

lint:                                ## Verify gofmt, run go vet and pinned Staticcheck
	files="$$(git ls-files '*.go')"; if [ -n "$$files" ]; then test -z "$$(gofmt -l $$files)"; fi
	go vet ./...
	go run honnef.co/go/tools/cmd/staticcheck@v0.7.0 ./...

test:                                ## Run race-enabled tests
	go test -race -count=1 ./...

cover:                               ## Race tests with atomic coverage profile and the 90% package/total gate
	mkdir -p coverage
	go test -race -count=1 -covermode=atomic -coverpkg=./internal/... -coverprofile=coverage/web.out ./...
	go run $(GO_TEST_COVERAGE) --config=.testcoverage.yml

build:                               ## Build the bursar-web binary into bin/
	go build -trimpath -ldflags "-s -w -X github.com/Zonastery/bursar/web/internal/version.Version=$(VERSION) -X github.com/Zonastery/bursar/web/internal/version.Commit=$(COMMIT) -X github.com/Zonastery/bursar/web/internal/version.Date=$(DATE)" -o bin/bursar-web ./cmd/bursar-web

docker-build:                        ## Build the bursar-web container image
	docker build --build-arg VERSION=$(VERSION) --build-arg COMMIT=$(COMMIT) --build-arg DATE=$(DATE) -t bursar-web:$(VERSION) .

ci: tidy-check lint test cover       ## Full CI gate: module tidiness, lint, tests, coverage
