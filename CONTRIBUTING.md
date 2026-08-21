# Contributing

bursar is a **monorepo** with three SDKs that must stay
behaviorally in sync:

- `python/` — the `bursar` package on PyPI (Pydantic models, `ast`-based safe
  expression engine, PostgreSQL-backed store).
- `javascript/` — the `@zonastery/bursar` package on npm (TypeScript mirror using
  `decimal.js`).
- `golang/` — the `github.com/Zonastery/bursar/golang/v2` Go package (Go mirror
  using `shopspring/decimal`).
- `tests/parity/expression_cases.json` (repo root) — a shared fixture loaded by
  **all** SDK test suites so a cross-SDK divergence fails CI.
- `docs/` — the Docusaurus + Sphinx/TypeDoc documentation site.

The SQL migrations bundled in `python/src/bursar/sql/*.sql` are the single source
of truth for the database schema; the JS integration tests apply the same files.

## Development Setup

### Python (`python/`)

```bash
git clone https://github.com/Zonastery/bursar.git
cd bursar/python
uv sync                       # runtime deps
uv sync --extra test          # ruff, pyright, pytest, testcontainers
# or, for the full dev group (notebooks, psycopg2, etc.):
uv sync --group dev
```

### JavaScript (`javascript/`)

```bash
cd bursar/javascript
bun ci                        # Bun 1.4.0; installs the committed bun.lock
```

The published SDK remains ESM for Node.js 22 or newer. Bun manages development
dependencies and scripts; consumers do not need Bun.

### Go (`golang/`)

```bash
cd golang
go mod download
go test ./...
```

The Go SDK supports Go 1.25 and 1.26. It is a versioned source module; use
`github.com/Zonastery/bursar/golang/v2` from applications. It deliberately provides no
CLI or schema migration command: use the Python `bursar` CLI for the shared SQL
baseline and tenant administration.

The optional Google ADK adapter is a separate module at
`golang/integrations/googleadk` because ADK requires Go 1.26.5. Keeping that
dependency isolated preserves the core SDK's Go 1.25 floor.

## Running Tests

### Python

```bash
cd python
uv run pytest                 # full suite
uv run pytest -q              # quiet
```

Store/manager/SQL **integration tests run against real PostgreSQL with
pg_partman 5 and pg_jsonschema 0.3**. They read the connection string from
`DATABASE_URL` or start a disposable PostgreSQL 17 testcontainer when Docker
is available. Without either, local database tests skip with a visible reason;
CI fails closed if the database cannot start. To run both SDK suites against
the repository's provider-neutral test image:

```bash
gmake test-integration
```

The fixtures apply `python/src/bursar/sql/*.sql` themselves; no preloaded Bursar
schema is needed. CI runs this matrix on Python 3.12 and 3.13.
Supplying `DATABASE_URL` requires `BURSAR_ALLOW_DATABASE_RESET=1` because the
harness truncates every Bursar-owned table between tests. Set it only for a
disposable test database; `gmake test-integration` handles this automatically
for its isolated container.

### JavaScript

```bash
cd javascript
bun run test                  # unit, parity, and available Postgres tests
BURSAR_ALLOW_DATABASE_RESET=1 \
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bursar bun run test
                              # also runs the PostgresStore integration tests
bun run typecheck             # typecheck
```

### Go

```bash
cd golang
go test -race ./...
go vet ./...
go run honnef.co/go/tools/cmd/staticcheck@v0.7.0 ./...

cd integrations/googleadk
go mod tidy
go test -race ./...
go vet ./...
go run honnef.co/go/tools/cmd/staticcheck@v0.7.0 ./...
```

The required coverage checks use the maintained, pinned `go-test-coverage`
tool and enforce at least 90% for every package and for each Go module overall.
From the repository root, run:

```bash
gmake test-go-coverage       # requires the bootstrapped PostgreSQL test database
gmake test-go-adk-coverage   # optional Google ADK module
```

`gmake test-integration` provisions both test tenants and runs both coverage
checks automatically. Coverage profiles are written below the ignored
`coverage/` directories and uploaded by CI for inspection.

## Code Style

### Python

- **Formatter / linter**: ruff (120-char line width, double quotes; rulesets
  E, F, I, N, W, UP, B, ASYNC, RUF100, SIM, RET, C901).
- **Type checker**: pyright (standard mode).

```bash
cd python
uv run ruff format src/ tests/ scripts/
uv run ruff check --fix src/ tests/ scripts/
uv run pyright src/
```

### JavaScript

- **Formatter**: prettier.
- **Linter**: eslint (typescript-eslint) + knip for dead-code detection.

```bash
cd javascript
bun run format:fix
bun run lint
bun run typecheck
```

### Go

- **Formatter**: `gofmt`.
- **Static analysis**: `go vet` + the pinned Staticcheck tool dependency.

```bash
cd golang
gofmt -w $(rg --files -g '*.go')
go vet ./...
go run honnef.co/go/tools/cmd/staticcheck@v0.7.0 ./...
```

### Git hooks (lefthook)

`lefthook.yml` (repo root) wires all SDKs:

- **pre-commit** — check-only `ruff`, Prettier, ESLint, `gofmt`, and SQLFluff runs on
  staged files, plus trailing-whitespace and merge-conflict-marker checks.
  Failed checks print the corresponding fix command so changes stay explicit
  and reviewable before they are re-staged.
- **pre-push** (parallel) — `pyright` + `pytest` for Python,
  `tsc --noEmit` + `vitest run` + `knip` for JavaScript, and
  `go vet` + Staticcheck + `go test -race` for Go.

Hooks are convenience only and are bypassable (`--no-verify`); **CI is the
authoritative gate.** Install them with `lefthook install`.

## Pull Request Process

1. Branch from `main`.
2. Make changes with descriptive commits (conventional-changelog style).
3. **Keep all SDKs in sync.** Any behavior change to one SDK must be
   mirrored in the others, and any new/changed expression or pricing behavior
   must have a matching case in `tests/parity/expression_cases.json` that passes
   in every SDK. Do not introduce a divergence.
4. Ensure all tests pass and there are no new type errors.
5. Open a PR against `main`.
6. CI runs lint → typecheck → test (Python 3.12–3.13 and Node 22/24 against
   PostgreSQL 16/17 + pg_partman 5 + pg_jsonschema 0.3; Go 1.25–1.26 unit and
   race tests, plus the required Go PostgreSQL package-smoke suite) and the
   cross-SDK parity gate.

## Adding Storage Backends (Python)

Implement the `CreditStore` ABC in `python/src/bursar/credits/`:

- All abstract methods declared in
  `python/src/bursar/credits/store.py` must be implemented (balance/credit ops,
  the atomic `deduct_with_allowance`, the
  `create_lease`/`settle_lease`/`release_lease`/`renew_lease` lease lifecycle,
  pricing/version management, plans, allowance/quota checks, refunds, expiry
  sweep, analytics, ledger/usage listing, and teams).
- Return the typed Pydantic models from `python/src/bursar/credits/types/`.
- Mirror the implementation in `javascript/src/credits/` against the
  `CreditStore` interface in `javascript/src/credits/store.ts`.
- Add unit tests and, for DB-backed stores, integration tests (see
  `python/tests/test_store_integration.py` and
  `javascript/tests/store-integration.test.ts`).

## Releasing

Releases are tag-triggered. The Python and npm packages are published from the
same tag via **OIDC trusted publishing** (no long-lived tokens); the Go module
is distributed directly from an immutable nested-module tag:

```bash
# tag and push (version must match python/pyproject.toml and javascript/package.json)
git tag v2.0.4
git push origin v2.0.4
```

On a `v*` tag, CI runs the full matrix, then the ordered publish jobs run under
a **protected `release` GitHub environment**:

- `release-go-tag` — creates or verifies the core and optional Google ADK Go
  module tags at the release commit.
- `release-pypi` — `uv build && uv publish` to PyPI via OIDC.
- `release-npm` — Bun installs and builds the SDK, then
  `npm publish --access public --provenance` publishes it via npm OIDC.

The Go SDK lives in `golang/` as the nested module
`github.com/Zonastery/bursar/golang/v2`. Its `/v2` semantic import suffix is
verified against the shared release version before anything is published. Once
the complete release preflight passes, `release-go-tag` creates
`golang/v2.0.4` and `golang/integrations/googleadk/v2.0.4` at the exact commit
referenced by `v2.0.4`. A rerun accepts an existing nested tag only when it
resolves to that same commit; a conflicting tag fails the release. Maintainers
create and push only the shared root tag—the workflow owns both nested tags. No
separate Go registry or release CLI is needed.

The ordered, idempotent jobs let maintainers recover from a registry outage by
rerunning the same immutable release, and the `release` environment allows an
approval requirement before publishing begins. Tag (release) runs are
explicitly **not** cancellable in the workflow concurrency config so a publish
cannot be killed mid-flight.

### One-time maintainer setup

- **PyPI trusted publisher** (<https://pypi.org/manage/account/publishing/>):

  | Field              | Value              |
  | ------------------ | ------------------ |
  | PyPI Project       | `bursar`           |
  | Owner / Repository | `Zonastery/bursar` |
  | Workflow name      | `ci.yml`           |
  | Environment        | `release`          |

- **npm trusted publisher**: configure the package's "Trusted publisher" on
  npmjs.com to point at this repo's `ci.yml` workflow / `release` environment.
- **GitHub environment**: create a `release` environment (Settings →
  Environments) and, ideally, add required reviewers and restrict it to tag
  refs.
- **Go tag permission**: keep Actions' `contents: write` permission enabled and,
  if repository rules protect tags, allow `ci.yml` to create `golang/v*` and
  `golang/integrations/googleadk/v*`. The release job fails closed if it cannot
  create or verify both nested tags.
