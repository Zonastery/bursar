# Bursar for Python

[![PyPI](https://img.shields.io/pypi/v/bursar.svg)](https://pypi.org/project/bursar/)
[![PyPI downloads](https://img.shields.io/pypi/dm/bursar.svg)](https://pypi.org/project/bursar/)

The Python SDK for [Bursar](https://github.com/Zonastery/bursar). It meters
usage, prices operations, and manages balances against the shared canonical
PostgreSQL schema and the same versioned configuration document as the
JavaScript SDK. Python 3.12 and 3.13 are supported.

## Installation

```bash
pip install bursar[postgres]
```

Extras: `postgres` (default recommended), `providers` (Stripe, Dodo),
`s3` (optional billing archive), and `test` (dev/test tooling).

Apply the SQL baseline before starting an application:

```bash
export DATABASE_URL=postgresql://...
bursar migrate
```

`bursar migrate` applies the ordered SQL files, records checksums, and is safe
to re-run. Repeat `--post-migrate-sql` to run idempotent host-owned SQL in the
same transaction.

## Usage

```python
from bursar import Bursar, PostgresStore

store = PostgresStore(database_url, tenant_id=tenant_id)
bursar = Bursar.create(credit_store=store)

grant = bursar.credits.add_credits(
    user_id, 500, entry_type="purchase", idempotency_key="checkout:42"
)
charge = bursar.credits.deduct_credits(
    user_id, 20, idempotency_key="job:42"
)
refund = bursar.credits.refund_credits(charge.entry_id)

page = bursar.credits.list_ledger_entries(user_id, limit=25)
while page.next_cursor is not None:
    page = bursar.credits.list_ledger_entries(
        user_id, limit=25, cursor=page.next_cursor
    )
```

`LedgerEntry`, `LedgerCursor`, and `LedgerPage` are exported from `bursar`;
pagination is cursor-only. `PostgresStore` is the production, tenant-scoped
store; `CreditStore` is the abstract base for custom implementations.

Publish one versioned configuration document through the facade — billing and
auto-recharge read the same active document:

```python
bursar.catalog.publish_and_activate(config)
```

## Optional S3 and ClickHouse storage

PostgreSQL remains authoritative. S3 and ClickHouse are optional delivery
targets, managed by `create_bursar_runtime` from `bursar.storage`:

```python
from bursar.storage import BursarRuntimeOptions, create_bursar_runtime

runtime = create_bursar_runtime(
    BursarRuntimeOptions(
        postgres=os.environ["DATABASE_URL"],
        tenant_id=os.environ["BURSAR_TENANT_ID"],
    )
)
runtime.start()
bursar = runtime.bursar
```

With no S3/ClickHouse configuration the runtime creates no background worker
and analytics query PostgreSQL directly. See the
[storage guide](https://zonastery.github.io/bursar/docs/guides/storage-backends)
for the full S3 and ClickHouse setup.

## Development

```bash
cd python
uv sync --group dev        # runtime + dev/test deps
uv run pytest              # full suite; integration tests need Postgres
ruff check src/ tests/
pyright src/
```

Real-Postgres tests resolve `DATABASE_URL`, else spin up a disposable
`postgres:16` testcontainer. See
[CONTRIBUTING.md](https://github.com/Zonastery/bursar/blob/main/CONTRIBUTING.md).

## License

AGPL-3.0.