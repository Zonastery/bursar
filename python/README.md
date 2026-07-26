# Bursar for Python

Python 3.12 and 3.13 are supported.

```bash
pip install bursar[postgres]
DATABASE_URL=postgresql://... bursar migrate
```

Create the reusable facade after migrations have run:

```python
from bursar import Bursar
from bursar.stores.postgres import PostgresStore

store = PostgresStore(database_url)
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
