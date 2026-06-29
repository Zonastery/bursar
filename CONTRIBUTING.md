# Contributing

ducto is a **monorepo** with two independently published SDKs that must stay
behaviorally in sync:

- `python/` — the `ducto` package on PyPI (Pydantic models, `ast`-based safe
  expression engine, Supabase/Postgres/in-memory stores).
- `javascript/` — the `@apoorwv/ducto` package on npm (TypeScript mirror using
  `decimal.js`).
- `tests/parity/expression_cases.json` (repo root) — a shared fixture loaded by
  **both** SDK test suites so a cross-SDK divergence fails CI.
- `docs/` — the Docusaurus + Sphinx/TypeDoc documentation site.

The SQL migrations bundled in `python/src/ducto/sql/*.sql` are the single source
of truth for the database schema; the JS integration tests apply the same files.

## Development Setup

### Python (`python/`)

```bash
git clone https://github.com/apoorwv/ducto.git
cd ducto/python
uv sync                       # runtime deps
uv sync --extra test          # ruff, pyright, pytest, pytest-postgresql
# or, for the full dev group (notebooks, psycopg2, etc.):
uv sync --group dev
```

### JavaScript (`javascript/`)

```bash
cd ducto/javascript
npm install
```

## Running Tests

### Python

```bash
cd python
uv run pytest                 # full suite
uv run pytest -q              # quiet
```

Store/manager/SQL **integration tests run against a real Postgres**. They read
the connection string from `DATABASE_URL` (falling back to the legacy
`DUCTO_TEST_PG_URL`); without one, the Postgres-backed tests *skip* but the
MemoryStore concurrency/double-spend tests still run. To run everything locally:

```bash
docker run -d --name ducto-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=ducto -p 5432:5432 postgres:16
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ducto uv run pytest
```

The test fixtures bootstrap the Supabase `auth` stubs/roles and apply
`python/src/ducto/sql/*.sql` themselves, so a bare `postgres:16` is enough. CI
runs this matrix on Python 3.11, 3.12, and 3.13.

### JavaScript

```bash
cd javascript
npx vitest run                # MemoryStore + parity always run
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ducto npx vitest run
                              # also runs the PostgresStore integration tests
npx tsc --noEmit              # typecheck
```

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
npm run format:fix
npm run lint
npx tsc --noEmit
```

### Git hooks (lefthook)

`lefthook.yml` (repo root) wires both SDKs:

- **pre-commit** — `ruff format`/`ruff check --fix` on staged Python files and
  `prettier --write`/`eslint --fix` on staged JS/TS files (auto-fixes are
  re-staged), plus trailing-whitespace and merge-conflict-marker checks.
- **pre-push** (parallel) — `pyright` + `pytest` for Python and
  `tsc --noEmit` + `vitest run` + `knip` for JavaScript.

Hooks are convenience only and are bypassable (`--no-verify`); **CI is the
authoritative gate.** Install them with `lefthook install`.

## Pull Request Process

1. Branch from `main`.
2. Make changes with descriptive commits (conventional-changelog style).
3. **Keep the two SDKs in sync.** Any behavior change to one SDK must be
   mirrored in the other, and any new/changed expression or pricing behavior
   must have a matching case in `tests/parity/expression_cases.json` that passes
   in both. Do not introduce a divergence.
4. Ensure all tests pass and there are no new type errors.
5. Open a PR against `main`.
6. CI runs lint → typecheck → test (Python 3.11–3.13, Node 18/20/22, both
   against `postgres:16`) and the cross-SDK parity gate.

## Adding Storage Backends (Python)

Implement the `CreditStore` ABC in `python/src/ducto/interface/`:

- All **29** abstract methods declared in
  `python/src/ducto/interface/base.py` must be implemented (balance/credit ops,
  the atomic `deduct_with_allowance`, the two-phase
  `reserve_credits`/`deduct_credits`, pricing/version management, plans,
  allowance/spend-cap checks, refunds, expiry sweep, analytics, transaction/
  usage listing, and teams).
- Return the typed Pydantic models from `python/src/ducto/interface/models.py`.
- Mirror the implementation in `javascript/src/stores/` against the
  `CreditStore` interface in `javascript/src/stores/credit-store.ts`.
- Add unit tests and, for DB-backed stores, integration tests (see
  `python/tests/test_store_integration.py` and
  `javascript/tests/store-integration.test.ts`).

## Releasing

Releases are tag-triggered. Both packages are published from the same tag via
**OIDC trusted publishing** (no long-lived tokens):

```bash
# tag and push (version must match python/pyproject.toml and javascript/package.json)
git tag v1.0.4
git push origin v1.0.4
```

On a `v*` tag, CI runs the full matrix, then two separate publish jobs run under
a **protected `release` GitHub environment**:

- `release-pypi` — `uv build && uv publish` to PyPI via OIDC.
- `release-npm` — `npm publish --access public --provenance` to npm via OIDC.

Splitting the jobs means a failure in one registry does not leave the other
half-published, and the `release` environment lets maintainers require approval
before any publish runs. Tag (release) runs are explicitly **not** cancellable
in the workflow concurrency config so a publish cannot be killed mid-flight.

### One-time maintainer setup

- **PyPI trusted publisher** (<https://pypi.org/manage/account/publishing/>):

  | Field | Value |
  |---|---|
  | PyPI Project | `ducto` |
  | Owner / Repository | `apoorwv/ducto` |
  | Workflow name | `ci.yml` |
  | Environment | `release` |

- **npm trusted publisher**: configure the package's "Trusted publisher" on
  npmjs.com to point at this repo's `ci.yml` workflow / `release` environment.
- **GitHub environment**: create a `release` environment (Settings →
  Environments) and, ideally, add required reviewers and restrict it to tag
  refs.
