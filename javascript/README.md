# Bursar JavaScript and TypeScript SDK for AI credits and usage billing

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
npm install @zonastery/bursar pg
```

Install `stripe` or `dodopayments` when using that payment provider. Other
optional peer dependencies are framework adapters
such as `@dodopayments/nextjs`, `@dodopayments/better-auth`, `next`, and
`better-auth`, and `@dodopayments/react-native-checkout`. No peer is required
for the database-free pricing core.

## Database setup

Database installation belongs to the SQL baseline, not to the SDK. Apply it
with the Python migration CLI:

```bash
python -m pip install "bursar[postgres]"
export BURSAR_MIGRATION_DATABASE_URL=postgresql://bursar_migrator@db.example.com/bursar
bursar migrate
```

The Python-only CLI is intentional. Install the same Bursar release as the
TypeScript SDK, run migrations with a dedicated migration principal, and use a
separate least-privilege `DATABASE_URL` in the application.

## Usage

```ts
import { Bursar, PostgresStore } from "@zonastery/bursar";

const store = new PostgresStore({
  postgres: process.env.DATABASE_URL!,
  tenantId,
  providerEnvironment: "test",
  connectionTimeoutMs: 10_000,
  statementTimeoutMs: 30_000,
  onPoolError: (error) => console.error("Bursar PostgreSQL pool error", error),
});
const bursar = new Bursar({ creditStore: store });

const grant = await bursar.credits.addCredits(userId, "500", {
  type: "purchase",
  idempotencyKey: "checkout:42",
});
const charge = await bursar.credits.deductCredits(userId, "20", {
  idempotencyKey: "job:42",
});
const refund = await bursar.credits.refundCredits(charge.entryId, {
  idempotencyKey: "refund:job:42",
});

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

## Framework integrations

Framework integrations are optional subpaths; the credits, metering, and
billing core does not import them. For Next.js, Dodo's official adapter verifies
and validates the request before Bursar maps the event:

```ts
// app/api/dodo/webhook/route.ts
import { createDodoNextWebhookHandler } from "@zonastery/bursar/providers/dodo/nextjs";

export const POST = createDodoNextWebhookHandler({
  webhookKey: process.env.DODO_PAYMENTS_WEBHOOK_SECRET!,
  getEventSink: () => bursar.requireBilling(),
});
```

React Native apps can compose a Bursar-created checkout intent with Dodo's
official native SDK. The app receives the checkout URL and persists only the
Bursar intent needed for recovery; API keys and payment confirmation remain on
the backend:

```ts
import { createDodoReactNativeCheckout } from "@zonastery/bursar/providers/dodo/react-native";

const checkout = createDodoReactNativeCheckout({
  returnUrl: "myappcheckout://return",
  store: pendingCheckoutStore,
  getCheckoutStatus: async (intentId) => (await api.checkoutStatus(intentId)).status,
});

const session = await api.createCheckout(input);
const result = await checkout.start(session); // UI hint only
await checkout.reconcile(); // authoritative Bursar status
```

Install `@dodopayments/react-native-checkout` directly in the native app, use
its Expo/native callback-scheme configuration, and forward React Native
`Linking` events to `checkout.handleOpenURL`. The adapter also exposes Dodo's
abandoned-session recovery without moving provider secrets into the app.

Better Auth users can compose Bursar with Dodo's official plugin. The Bursar
plugin provisions accounts and adopts the `dodoCustomerId` maintained by the
official adapter, avoiding two customer owners:

```ts
import { dodopayments, portal } from "@dodopayments/better-auth";
import { bursarBetterAuth } from "@zonastery/bursar/integrations/better-auth";
import {
  dodoBetterAuthCustomer,
  dodoBetterAuthWebhooks,
} from "@zonastery/bursar/providers/dodo/better-auth";

plugins: [
  dodopayments({
    client: dodoClient,
    createCustomerOnSignUp: true,
    use: [
      portal(),
      dodoBetterAuthWebhooks({
        webhookKey: process.env.DODO_PAYMENTS_WEBHOOK_SECRET!,
        getEventSink: () => bursar.requireBilling(),
      }),
    ],
  }),
  bursarBetterAuth({
    getBursar: () => bursar,
    resolveProviderCustomer: dodoBetterAuthCustomer,
  }),
];
```

Run Better Auth's schema generator after enabling the official Dodo plugin; it
adds the optional `dodoCustomerId` user field. Checkout and portal operations
may still call `bursar.requireCommerce()` when the application needs Bursar's
checkout intents, provider selection, or idempotency guarantees.

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
  operatorPostgres: process.env.BURSAR_OPERATOR_DATABASE_URL!,
  tenantId: process.env.BURSAR_TENANT_ID!,
  providerEnvironment: "test",
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
bun ci                        # Bun 1.4.0; installs the committed bun.lock
bun run typecheck
bun run test                  # integration tests use a disposable testcontainer by default
bun run lint
bun run build
```

Bun manages development dependencies and scripts; consumers of the published
package do not need it. See
[CONTRIBUTING.md](https://github.com/Zonastery/bursar/blob/main/CONTRIBUTING.md).
An externally supplied `DATABASE_URL` also requires
`BURSAR_ALLOW_DATABASE_RESET=1`; the integration harness truncates Bursar data.

## License

AGPL-3.0.
