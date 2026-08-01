# Bursar

Bursar is Zonastery's reusable credit-ledger and billing SDK. Python and
JavaScript expose the same application boundary: a `Bursar` facade with
`credits`, `catalog`, and optional `billing` capabilities.

The PostgreSQL model is intentionally canonical:

- `credit_accounts.balance` is the only stored account balance.
- `credit_ledger_entries` is the only monetary history.
- `credit_lots` and `credit_lot_allocations` determine bucket availability and
  expiry.
- `credit_leases` reserve credits for work that may settle or be released.
- `account_plan_assignments` links an account to its active plan.

There are no projected transaction or bucket-balance tables.

## Install and migrate

```bash
pip install bursar[postgres]
export DATABASE_URL=postgresql://...
bursar migrate
bursar tenant create acme --id 018f7f5f-7b4a-7000-8000-000000000001
```

The migration runner records checksums and is safe to run again. Existing
pre-release databases must drop and recreate the `bursar` schema before this
greenfield baseline; no conversion migration is supplied.

Hosts can install trusted, idempotent integration SQL in the same transaction:

```bash
bursar migrate --post-migrate-sql ./host-integration.sql
```

Repeat `--post-migrate-sql` to apply multiple files in order. Host files are
executed on every run and are not recorded in Bursar's migration ledger.

```python
from bursar import Bursar, PostgresStore

store = PostgresStore(database_url, tenant_id=tenant_id)
bursar = Bursar.create(credit_store=store)

added = bursar.credits.add_credits(user_id, 1_000, entry_type="purchase")
charged = bursar.credits.deduct_credits(user_id, 25)
entry = bursar.credits.get_ledger_entry(user_id, charged.entry_id)
page = bursar.credits.list_ledger_entries(user_id, limit=50)
next_page = bursar.credits.list_ledger_entries(
    user_id, limit=50, cursor=page.next_cursor
)
```

```ts
import { Bursar, PostgresStore } from "@zonastery/bursar";

const store = new PostgresStore(process.env.DATABASE_URL!, tenantId);
const bursar = new Bursar({ creditStore: store });

const added = await bursar.credits.addCredits(userId, 1_000, {
  type: "purchase",
});
const charged = await bursar.credits.deductCredits(userId, 25);
const entry = await bursar.credits.getLedgerEntry(userId, charged.entryId);
const page = await bursar.credits.listLedgerEntries(userId, { limit: 50 });
const nextPage = page.nextCursor
  ? await bursar.credits.listLedgerEntries(userId, {
      limit: 50,
      cursor: page.nextCursor,
    })
  : null;
```

Database installation belongs to `bursar migrate`, not to stores or the
facade.

## Canonical configuration

Publish one strict, versioned configuration document. The sections are
`pricing`, `credits`, `entitlements`, `admission`, `plans`, `commerce`, and
`catalog`; only `version` and `credits` are required. Unknown historical shapes
are rejected.

```yaml
version: 1

pricing:
  operations:
    completion:
      measures:
        input_tokens: { unit: token }
        output_tokens: { unit: token }
      dimensions:
        model: { type: string }
  rate_cards:
    standard:
      operations:
        completion:
          unmatched:
            action: charge
            charge:
              type: sum
              components:
                - { type: per_unit, measure: input_tokens, rate: "0.05" }
                - { type: per_unit, measure: output_tokens, rate: "0.10" }

credits:
  buckets:
    purchased:
      priority: 10
  default_bucket: purchased

plans:
  free:
    display_name: Free
    rate_card: standard
    allowed_operations: [completion]
```

Publish and activate through `bursar.catalog`. Billing and auto-recharge always
read the active canonical configuration.

See the [Python package](python/README.md), [JavaScript package](javascript/README.md),
and [documentation site](docs/docs/intro.mdx).
