# Contributing

bursar is a **monorepo** with two independently published SDKs that must stay
behaviorally in sync:

- `python/` — the `bursar` package on PyPI (Pydantic models, `ast`-based safe
  expression engine, PostgreSQL-backed store).
- `javascript/` — the `@zonastery/bursar` package on npm (TypeScript mirror using
  `decimal.js`).
- `tests/parity/expression_cases.json` (repo root) — a shared fixture loaded by
  **both** SDK test suites so a cross-SDK divergence fails CI.
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
bun ci                        # Bun 1.3.14; installs the committed bun.lock
```

The published SDK remains ESM for Node.js 22 or newer. Bun manages development
dependencies and scripts; consumers do not need Bun.

## Running Tests

### Python

```bash
cd python
uv run pytest                 # full suite
uv run pytest -q              # quiet
```

Store/manager/SQL **integration tests run against real PostgreSQL with
pg_partman 5 and pg_jsonschema 0.3**. They read the connection string from
`DATABASE_URL` (falling back to the legacy `BURSAR_TEST_PG_URL`) or start a
disposable PostgreSQL 17 testcontainer when Docker is available. Without
either, database tests skip with a visible reason. To run everything locally:

```bash
docker run -d --name bursar-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=bursar -p 5432:5432 \
  public.ecr.aws/supabase/postgres:17.6.1.156
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bursar uv run pytest
```

The test fixtures bootstrap the Supabase `auth` stubs/roles and apply
`python/src/bursar/sql/*.sql` themselves; no preloaded Bursar schema is needed.
CI runs this matrix on Python 3.12 and 3.13.

### JavaScript

```bash
cd javascript
bun run test                  # unit, parity, and available Postgres tests
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bursar bun run test
                              # also runs the PostgresStore integration tests
bun run typecheck             # typecheck
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
bun run format:fix
bun run lint
bun run typecheck
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
6. CI runs lint → typecheck → test (Python 3.12–3.13, Node 22/24, both
   against PostgreSQL 17 + pg_partman 5 + pg_jsonschema 0.3) and the
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

Releases are tag-triggered. Both packages are published from the same tag via
**OIDC trusted publishing** (no long-lived tokens):

```bash
# tag and push (version must match python/pyproject.toml and javascript/package.json)
git tag v2.0.0
git push origin v2.0.0
```

On a `v*` tag, CI runs the full matrix, then two separate publish jobs run under
a **protected `release` GitHub environment**:

- `release-pypi` — `uv build && uv publish` to PyPI via OIDC.
- `release-npm` — Bun installs and builds the SDK, then
  `npm publish --access public --provenance` publishes it via npm OIDC.

Splitting the jobs means a failure in one registry does not leave the other
half-published, and the `release` environment lets maintainers require approval
before any publish runs. Tag (release) runs are explicitly **not** cancellable
in the workflow concurrency config so a publish cannot be killed mid-flight.

### One-time maintainer setup

- **PyPI trusted publisher** (<https://pypi.org/manage/account/publishing/>):

  | Field | Value |
  |---|---|
  | PyPI Project | `bursar` |
  | Owner / Repository | `Zonastery/bursar` |
  | Workflow name | `ci.yml` |
  | Environment | `release` |

- **npm trusted publisher**: configure the package's "Trusted publisher" on
  npmjs.com to point at this repo's `ci.yml` workflow / `release` environment.
- **GitHub environment**: create a `release` environment (Settings →
  Environments) and, ideally, add required reviewers and restrict it to tag
  refs.
