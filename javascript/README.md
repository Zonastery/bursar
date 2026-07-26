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

const grant = await bursar.credits.addCredits(
  userId,
  500,
  { type: "purchase", idempotencyKey: "checkout:42" },
);
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
