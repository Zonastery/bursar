# Bursar — Getting Started

Bursar is a declarative credit calculation and billing engine for AI SaaS:
an exact-decimal credit ledger, prepaid credits, plans and allowances,
subscriptions, and quotas — all driven by one strict, versioned
`BursarConfig` document, with Python and TypeScript SDKs over a single
PostgreSQL schema. This file is the zero-to-working-ledger path: schema →
tenant → config → first credits.

## Prerequisites

- PostgreSQL 16+ reachable from `DATABASE_URL`, with **pg_partman 5.x** and
  **pg_jsonschema 0.3+** available to the migration role. Bursar installs
  them in the `partman` and `extensions` schemas.
- Python 3.12+ (SDK) or Node.js 22+ (SDK).
- The `bursar` CLI (ships with the Python package).

## Install

```bash
pip install bursar[postgres]      # Python — the `postgres` extra is required
```

```bash
npm install @zonastery/bursar     # TypeScript
```

Import `PostgresStore` and the `CreditStore` base from the package top level
(`from bursar import CreditStore, PostgresStore` / `import { CreditStore,
PostgresStore } from "@zonastery/bursar"`). `PostgresStore` is the only
production credit store — there is no in-memory or HTTP store anymore. All
account state lives in PostgreSQL. Optional ClickHouse/S3 adapters and the
runtime composition root import from `bursar.storage` (Python) and the
`@zonastery/bursar/node` subpath (TypeScript).

## 1. Apply the schema

```bash
export DATABASE_URL=postgresql://...
bursar migrate
```

`bursar migrate` applies the ordered SQL baseline, records a checksum for
every file, and **fails if an already-applied file changed**. It is safe to
re-run — a second run is a no-op. Optionally pass repeatable
`--post-migrate-sql ./file.sql` files that execute under the same advisory
lock and transaction (a failure rolls back everything); those are not
checksummed and must be idempotent.

## 2. Create a tenant

```bash
bursar tenant create acme
# 018f7f5f-7b4a-7000-8000-000000000001
export BURSAR_TENANT_ID=018f7f5f-7b4a-7000-8000-000000000001
```

`tenant create` generates the UUID when `--id` is omitted and prints it
(`--display-name` is optional). The printed UUID is your **tenant id**; the
store and every runtime bind it at construction time. `BURSAR_TENANT_ID` is
required by all `bursar config` subcommands. Lifecycle state changes with
`bursar tenant status <id> suspended|active`.

For a deployment bootstrap, one idempotent command provisions the tenant
**and** publishes its initial config (validating the config first):

```bash
export BURSAR_TENANT_ID=018f7f5f-7b4a-7000-8000-000000000001
bursar tenant bootstrap acme ./pricing.yaml --display-name "Acme"
```

Never insert into `bursar.tenants` from host migrations or seed SQL — tenant
storage is a Bursar implementation detail behind the operator CLI.

## 3. Write and publish a pricing config

One strict, versioned document drives pricing, buckets, and plans. Save it
as `pricing.yaml`:

```yaml
version: 1
catalog:
  default_plan: free
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
          rules:
            - when:
                model:
                  op: in
                  values: [gpt-4o, gpt-4o-mini]
              charge:
                type: per_unit
                measure: input_tokens
                rate: "0.0025"
                unit_size: "1000000"
          unmatched:
            action: reject
credits:
  buckets:
    promotional: { priority: 1 }
    purchased: { priority: 10 }
  default_bucket: purchased
plans:
  free:
    display_name: Free
    rate_card: standard
    allowed_operations: [completion]
    credit_allowance:
      amount: "10000"
      priority: 5
      window: { type: calendar, unit: month, count: 1 }
```

**The schema is strict**: unknown fields are rejected, and every exact
decimal value is a string. Only `version` and `credits` are required; fixed
v1 accounting defaults to `credit`, six decimal places, half-up rounding.
Rate matcher operators must agree with their dimension type (`prefix` for
strings, `range` for numbers).

Validate, then publish:

```bash
bursar config validate pricing.yaml   # --json for CI diagnostics
bursar config set pricing.yaml
```

`config set` validates again, creates an **immutable** version, and
activates it. A `set` whose payload is identical to the active version
reports "No changes" and does not churn versions. Check with:

```bash
bursar config get    # active version as JSON
bursar config list   # all versions, * marks active
```

To roll back a bad config, `bursar config activate <previous-version>` —
versions are immutable, so the prior document is exactly as published.

## 4. Construct the facade

Connect the store to your tenant and build the facade:

```python
from bursar import Bursar, PostgresStore

store = PostgresStore(database_url, tenant_id=tenant_id)
bursar = Bursar.create(credit_store=store)
```

```ts
import { Bursar, PostgresStore } from "@zonastery/bursar";

const store = new PostgresStore(process.env.DATABASE_URL!, tenantId);
const bursar = new Bursar({ creditStore: store });
```

The facade owns `credits`, `catalog`, `accounts`, and (optionally)
`billing`/`commerce`. Do not construct lifecycle services independently.

## 5. First credits

Register a user and give her an account, a plan, and some credits. The free
plan carries a 10,000-credit monthly allowance:

```python
ada = "11111111-1111-4111-8111-111111111111"

bursar.accounts.on_account_created(ada, event_key="signup")
bursar.credits.add_credits(
    ada,
    1000,
    entry_type="purchase",
    idempotency_key="purchase:bootstrap",
)

balance = bursar.credits.get_balance(ada)
print(balance.balance, balance.lifetime_purchased)
```

```ts
const ada = "11111111-1111-4111-8111-111111111111";

await bursar.accounts.onAccountCreated({ accountId: ada, eventKey: "signup" });
await bursar.credits.addCredits(ada, 1000, {
  type: "purchase",
  idempotencyKey: "purchase:bootstrap",
});

const balance = await bursar.credits.getBalance(ada);
console.log(balance.balance, balance.lifetimePurchased);
```

`on_account_created` / `onAccountCreated` assigns the catalog's default plan
(`catalog.default_plan`, else the lowest-rank plan) and runs eligible
`account_created` grant programs in one idempotent call. `add_credits` with
`entry_type="purchase"` grants into the default bucket and appends a
`purchase` ledger entry.

## 6. Where to go next

- [`credit-lifecycle.md`](./credit-lifecycle.md) — ledger model, charging,
  leases, allowances, quotas, expiry.
- [`financial-safety.md`](./financial-safety.md) — invariants, idempotency,
  multitenancy, error taxonomy.
- Docs: https://zonastery.github.io/bursar/docs/quickstart ·
  `/docs/cli` · `/docs/guides/credit-lifecycle` ·
  `/docs/guides/financial-safety` · `/docs/guides/multitenancy` ·
  `/docs/concepts/data-model` · `/docs/concepts/configuration` ·
  `/docs/concepts/plans`.
- Notebooks: `/docs/notebooks/01_first_pricing_config` and
  `/docs/notebooks/04_credit_lifecycle` walk the same story end to end.
