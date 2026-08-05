# Bursar for JavaScript

[![npm](https://img.shields.io/npm/v/@zonastery/bursar.svg)](https://www.npmjs.com/package/@zonastery/bursar)
[![npm downloads](https://img.shields.io/npm/dm/@zonastery/bursar.svg)](https://www.npmjs.com/package/@zonastery/bursar)

<p align="center">
  <img
    src="https://raw.githubusercontent.com/zonastery/bursar/main/docs/static/img/logo.png"
    alt="Bursar logo"
    width="192"
    height="192"
  />
</p>

The TypeScript SDK for [Bursar](https://github.com/Zonastery/bursar) — a
behavioral mirror of the Python SDK. It meters usage, prices operations, and
manages balances against the shared canonical PostgreSQL schema and the same
versioned configuration document, with identical rounding. `@zonastery/bursar`
is ESM-only and requires Node.js 22 or newer.

## Installation

```bash
npm install @zonastery/bursar
```

Optional peer dependencies: `pg` (PostgresStore), `stripe` and `dodopayments`
(provider billing), `js-yaml` (YAML config loading). No peer is required for
core credits.

## Database setup

Database installation belongs to the SQL baseline, not to the SDK. Apply it
with the Python migration CLI:

```bash
pip install bursar[postgres]
export DATABASE_URL=postgresql://...
bursar migrate
```

## Usage

```ts
import { Bursar, PostgresStore } from "@zonastery/bursar";

const store = new PostgresStore(process.env.DATABASE_URL!, tenantId, {
  connectionTimeoutMs: 10_000,
  statementTimeoutMs: 30_000,
  onPoolError: (error) => console.error("Bursar PostgreSQL pool error", error),
});
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

`LedgerEntry`, `LedgerCursor`, and `LedgerPage` are the public history
contract; pagination is cursor-only. Publish one versioned configuration
document through the facade — billing and auto-recharge read the same active
document:

```ts
await bursar.catalog.publishAndActivate(config);
```

## Errors, deadlines, and retries

All Bursar-classified failures extend `BursarError` and carry stable `code`,
`category`, and `retryable` fields. Native `cause` chains retain the underlying
driver error without exposing it from
`bursarErrorPublicMessage()` or the serialized `toJSON()` shape. PostgreSQL
failures are classified primarily from SQLSTATE and network error codes, with
narrow fallbacks for the stable timeout messages emitted by `pg` itself.

`PostgresStore` and `PostgresBillingStore` use a 10-second connection deadline,
a 30-second server-side statement deadline, and a 30-second idle-transaction
deadline by default. All are configurable. An SDK-owned pool also installs an
idle-client error listener so a network partition cannot become an uncaught
EventEmitter error.

Use `retryBursarOperation()` only for reads or idempotent mutations. It uses
bounded exponential backoff with jitter, an elapsed-time budget, and optional
`AbortSignal` cancellation:

```ts
import { isBursarError, retryBursarOperation } from "@zonastery/bursar";

try {
  const balance = await retryBursarOperation(() => bursar.credits.getBalance(userId), {
    maxAttempts: 3,
    signal: request.signal,
  });
} catch (error) {
  if (isBursarError(error)) {
    console.error("Bursar request failed", error.toJSON());
  }
  throw error;
}
```

`StoreError.indeterminate` means a transport failure occurred when a mutation
may have reached PostgreSQL. Retry only with the same idempotency key. A plain
`StoreError` is deliberately non-retryable; custom stores should throw
`StoreUnavailableError` or `StoreTimeoutError` only for classified transient
failures.

## Optional S3 and ClickHouse storage

PostgreSQL remains authoritative. S3 and ClickHouse are optional delivery
targets, managed by `createBursarRuntime` from `@zonastery/bursar/node`:

```ts
import { createBursarRuntime } from "@zonastery/bursar/node";

const runtime = await createBursarRuntime({
  postgres: process.env.DATABASE_URL!,
  tenantId: process.env.BURSAR_TENANT_ID!,
});
await runtime.start();
const bursar = runtime.bursar;
```

With no S3/ClickHouse configuration the runtime creates no background worker
and analytics query PostgreSQL directly. See the
[storage guide](https://zonastery.github.io/bursar/docs/guides/storage-backends)
for the full S3 and ClickHouse setup.

## Development

```bash
cd javascript
bun ci                        # Bun 1.3.14; installs the committed bun.lock
bun run typecheck
bun run test                  # integration tests need Postgres (DATABASE_URL or testcontainer)
bun run lint
bun run build
```

Bun manages development dependencies and scripts; consumers of the published
package do not need it. See
[CONTRIBUTING.md](https://github.com/Zonastery/bursar/blob/main/CONTRIBUTING.md).

## License

AGPL-3.0.
