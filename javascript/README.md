# Bursar for JavaScript

`@zonastery/bursar` is ESM-only and requires Node.js 22 or newer.

Apply the SQL baseline with the Python migration CLI before starting an
application:

```bash
DATABASE_URL=postgresql://... bursar migrate
```

```ts
import { Bursar, PostgresStore } from "@zonastery/bursar";

const store = new PostgresStore(process.env.DATABASE_URL!);
const bursar = new Bursar({ creditStore: store });

const grant = await bursar.credits.addCredits(userId, 500, {
  type: "purchase",
  idempotencyKey: "checkout:42",
});
const charge = await bursar.credits.deductCredits(userId, 20, {
  idempotencyKey: "job:42",
});
const refund = await bursar.credits.refundCredits(charge.entryId);

let page = await bursar.credits.listLedgerEntries(userId, { limit: 25 });
while (page.nextCursor) {
  page = await bursar.credits.listLedgerEntries(userId, {
    limit: 25,
    cursor: page.nextCursor,
  });
}
```

The public history contract consists of `LedgerEntry`, `LedgerCursor`, and
`LedgerPage`. Pagination is cursor-only. Use `getLedgerEntry` for one entry and
`listUsageEntries` for the usage-only view.

`bursar.catalog.publishAndActivate(config)` publishes a canonical configuration
with `usage`, `credits`, `plans`, and `payments`. Billing and auto-recharge read
that active configuration; there is no separate billing configuration.

The package keeps the `CreditStore` abstraction and `PostgresStore` for custom
applications. Database installation is deliberately not a store or `Bursar`
method.

## Optional S3 and ClickHouse storage

`createBursarRuntime` is the Node composition root when Bursar should manage
optional storage projections. PostgreSQL remains authoritative for balances,
leases, billing state, and the transactional outbox.

With no extra infrastructure, it creates no background worker and analytics
continue to query PostgreSQL:

```ts
import { createBursarRuntime } from "@zonastery/bursar/node";

const runtime = await createBursarRuntime({
  postgres: process.env.DATABASE_URL!,
});
await runtime.start();

const bursar = runtime.bursar;
```

S3 connection settings and a structural ClickHouse client can be added when
those projections are needed:

```ts
import { createClient } from "@clickhouse/client";
import { createBursarRuntime } from "@zonastery/bursar/node";

const clickhouseClient = createClient({ url: process.env.CLICKHOUSE_URL! });

const runtime = await createBursarRuntime({
  postgres: process.env.DATABASE_URL!,
  s3: {
    bucket: process.env.BURSAR_S3_BUCKET!,
    region: process.env.BURSAR_S3_REGION!,
    endpoint: process.env.BURSAR_S3_ENDPOINT,
    forcePathStyle: process.env.BURSAR_S3_FORCE_PATH_STYLE === "true",
    credentials: {
      accessKeyId: process.env.BURSAR_S3_ACCESS_KEY_ID!,
      secretAccessKey: process.env.BURSAR_S3_SECRET_ACCESS_KEY!,
    },
  },
  clickhouse: {
    client: clickhouseClient,
    // Optional; omit to keep the ClickHouse projection indefinitely.
    retentionDays: 730,
  },
});

await runtime.start();
// Use runtime.bursar in the application.
// On graceful shutdown:
await runtime.close();
```

External writes happen through a leased PostgreSQL outbox, never in a customer
request. S3 object keys are deterministic and the ClickHouse projection is
replay-safe. ClickHouse analytics are therefore eventually consistent.
PostgreSQL payload retention should be at least as long as the outbox retry
horizon, which is enforced by the SQL storage configuration.

Use `outbox: false` only when a separate process consumes the Bursar outbox.
Database retention maintenance remains independent: schedule
`bursar.maybe_run_storage_maintenance()` and partition maintenance with
`pg_cron` as described in the SQL README.
