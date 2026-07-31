# Bursar for Python

Python 3.12 and 3.13 are supported.

```bash
pip install bursar[postgres]
DATABASE_URL=postgresql://... bursar migrate
```

To install trusted host-owned database objects after Bursar, pass one or more
repeatable integration files:

```bash
DATABASE_URL=postgresql://... \
  bursar migrate --post-migrate-sql ./host-integration.sql
```

The files run in order, after Bursar's pending migrations and in the same
transaction. They run on every invocation, so they must be idempotent.

Create the reusable facade after migrations have run:

```python
from bursar import Bursar
from bursar.stores.postgres import PostgresStore

store = PostgresStore(database_url, tenant_id=tenant_id)
bursar = Bursar.create(credit_store=store)
```

`bursar.credits` owns account operations:

```python
grant = bursar.credits.add_credits(
    user_id,
    500,
    entry_type="purchase",
    idempotency_key="checkout:42",
)
charge = bursar.credits.deduct_credits(
    user_id,
    20,
    idempotency_key="job:42",
)
refund = bursar.credits.refund_credits(charge.entry_id)

page = bursar.credits.list_ledger_entries(user_id, limit=25)
while page.next_cursor is not None:
    page = bursar.credits.list_ledger_entries(
        user_id, limit=25, cursor=page.next_cursor
    )
```

`LedgerEntry`, `LedgerCursor`, and `LedgerPage` are exported from `bursar`.
Pagination is cursor-only. Usage history is available through
`list_usage_entries`; one entry is available through `get_ledger_entry`.

The store modules live under `bursar.stores`:

- `bursar.stores.postgres.PostgresStore`
- `bursar.stores.memory.MemoryStore`
- `bursar.stores.supabase.HttpxSupabaseStore`
- `bursar.stores.base.CreditStore` for custom implementations

Use `bursar.catalog.publish_and_activate(config)` for a canonical document with
`usage`, `credits`, `plans`, and `payments`. The optional billing service and
auto-recharge policy read that same active document.

Stores and `Bursar` do not install database objects. Deployment is:

```text
DATABASE_URL -> bursar migrate -> publish canonical config -> start app
```

## Optional S3 and ClickHouse storage

`create_bursar_runtime` is the Python composition root when Bursar should
manage optional storage projections. PostgreSQL remains authoritative for
balances, leases, billing state, and the transactional outbox.

With no extra infrastructure, it creates no background worker and analytics
continue to query PostgreSQL:

```python
import os

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

Install `bursar[postgres,s3]` and add S3 connection settings when an archive is
needed. ClickHouse remains structurally injected:

```python
import os

import clickhouse_connect

from bursar.storage import (
    BursarRuntimeOptions,
    ClickHouseUsageStoreOptions,
    S3BillingArchiveOptions,
    S3Credentials,
    create_bursar_runtime,
)

clickhouse_client = clickhouse_connect.get_client(
    dsn=os.environ["CLICKHOUSE_URL"]
)

runtime = create_bursar_runtime(
    BursarRuntimeOptions(
        postgres=os.environ["DATABASE_URL"],
        s3=S3BillingArchiveOptions(
            bucket=os.environ["BURSAR_S3_BUCKET"],
            region=os.environ["BURSAR_S3_REGION"],
            endpoint=os.getenv("BURSAR_S3_ENDPOINT"),
            force_path_style=os.getenv("BURSAR_S3_FORCE_PATH_STYLE") == "true",
            credentials=S3Credentials(
                access_key_id=os.environ["BURSAR_S3_ACCESS_KEY_ID"],
                secret_access_key=os.environ["BURSAR_S3_SECRET_ACCESS_KEY"],
            ),
        ),
        clickhouse=ClickHouseUsageStoreOptions(
            client=clickhouse_client,
            # Optional; omit to retain the projection indefinitely.
            retention_days=730,
        ),
    )
)

runtime.start()
# Use runtime.bursar in the application.
# On graceful shutdown:
runtime.close()
```

External writes happen through a leased PostgreSQL outbox, never in a customer
request. S3 object keys are deterministic and the ClickHouse projection is
replay-safe. ClickHouse analytics are therefore eventually consistent.
PostgreSQL payload retention should be at least as long as the outbox retry
horizon, which is enforced by the SQL storage configuration.

Set `outbox=False` only when a separate process consumes the Bursar outbox.
Database retention maintenance remains independent: schedule
`bursar.maybe_run_storage_maintenance()` and partition maintenance with
`pg_cron` as described in the SQL README.
