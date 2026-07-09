# SDK Integration — `ManagedBursarStore`

A new store implementation for the JS bursar SDK that connects to Service 2 instead of directly to Supabase. Lives in `javascript/src/stores/managed-store.ts`.

## Usage

```ts
import { CreditManager, ManagedBursarStore } from "@zonastery/bursar";

const store = new ManagedBursarStore({
  baseUrl: "https://api.bursar.cloud",
  apiKey: process.env.BURSAR_KEY!,      // sk_live_<keyId>_<secret>
  pricingCacheTtl: 300,                 // seconds, same as HttpxSupabaseStore default
  fetch: globalThis.fetch,              // injectable for testing/proxy
});

const manager = new CreditManager(store);
await manager.deduct("user_abc", { model: "gpt-4", inputTokens: 500 });
```

## Interface

```ts
interface ManagedBursarStoreOptions {
  baseUrl: string;          // e.g. "https://api.bursar.cloud"
  apiKey: string;           // sk_live_<keyId>_<secret>
  pricingCacheTtl?: number; // default 300
  fetch?: typeof fetch;     // default globalThis.fetch
  timeout?: number;         // request timeout ms, default 30000
}
```

## Method mapping

Each abstract method of `CreditStore` (`javascript/src/stores/credit-store.ts:100`) maps to a Service 2 HTTP call.

| CreditStore method | HTTP call |
|---|---|
| `getBalance(userId)` | `GET /v1/users/:userId/balance` |
| `getAvailable(userId)` | `GET /v1/users/:userId/available` |
| `getBucketBalances(userId)` | `GET /v1/users/:userId/buckets` |
| `addCredits(userId, amount, type?, metadata?, expiresAt?, bucket?, idempotencyKey?)` | `POST /v1/users/:userId/credits/add` |
| `deductWithAllowance(userId, amount, …)` — not called directly by manager; see **Cost calculation** below | _(called by CreditManager.deduct, which handles calculation)_ |
| `createLease(userId, amountOrMetrics, operationType?, billingMode?, …)` | `POST /v1/users/:userId/leases` |
| `settleLease(userId, leaseId, amountOrMetrics, …)` | `POST /v1/users/:userId/leases/:leaseId/settle` |
| `releaseLease(userId, leaseId)` | `POST /v1/users/:userId/leases/:leaseId/release` |
| `renewLease(userId, leaseId, ttl)` | `POST /v1/users/:userId/leases/:leaseId/renew` |
| `getUserPlan(userId)` | `GET /v1/users/:userId/plan` |
| `setUserPlan(userId, planKey, planAssignedAt?)` | `PUT /v1/users/:userId/plan` |
| `unsetUserPlan(userId)` | `DELETE /v1/users/:userId/plan` |
| `checkFeature(userId, feature)` | `GET /v1/users/:userId/features/:feature` |
| `checkFeatureLimit(userId, feature, maxCalls, periodStart, periodEnd)` | `GET /v1/users/:userId/features/:feature/limit` |
| `checkAllowance(userId, periodStart?)` | `GET /v1/users/:userId/check-allowance` |
| `checkSpendCap(userId, model?, amount?)` | _(via spend caps API — or merged into deductWithAllowance)_ |
| `getActivePricing()` | `GET /v1/config/active` |
| `getPricingHistory()` | `GET /v1/config/versions` |
| `getPricingConfig(version)` | `GET /v1/config/versions/:version` |
| `activatePricing(version)` | ❌ not available on data plane (admin-only) |
| `setActivePricing(config)` | ❌ not available on data plane (admin-only) |
| `refundCredits(transactionId, amount?)` | `POST /v1/transactions/:txId/refund` |
| `sweepExpiredCredits(dryRun?, userId?)` | `POST /v1/credits/sweep` |
| `revokeCreditsByTxType(userId, txType)` | `POST /v1/users/:userId/credits/revoke` _— needs adding_ |
| `incrementUsageWindow(userId, planId, amount)` | _(internal, via deductWithAllowance)_ |
| `createTeam(name, initialBalance?)` | `POST /v1/teams` |
| `getTeamBalance(teamId)` | `GET /v1/teams/:teamId/balance` |
| `addTeamMember(teamId, userId, role?, spendCap?)` | `POST /v1/teams/:teamId/members` |
| `getTeamMembers(teamId)` | `GET /v1/teams/:teamId/members` |
| `deductTeam(teamId, userId, metrics, …)` | `POST /v1/teams/:teamId/deduct` |
| _Analytics methods (spendByUser, etc.)_ | `GET /v1/analytics/…` |
| _Transaction listing_ | `GET /v1/users/:userId/transactions` |
| `listUsageEvents(userId)` | `GET /v1/users/:userId/usage-events` |
| `setup(databaseUrl?)` | `POST /v1/setup` — _delegates to tenant's migration/setup_ |

## Cost calculation — the important change

In the direct-Supabase model, the SDK's `CreditManager.deduct()`:

1. Calls `PricingEngine.calculate(metrics)` to get the cost.
2. Calls `store.deductWithAllowance(userId, amount, …)` with the calculated amount.

With `ManagedBursarStore`, the SDK **still calculates** the cost (for the `canAfford` check and for display), but Service 2 **recalculates** and ignores the SDK-sent amount. This means:

1. `CreditManager.deduct(userId, metrics)` calls `PricingEngine.calculate(metrics)` on the SDK side — uses cached pricing config. Fast, no network.
2. `CreditManager.deduct()` then calls `store.deductWithAllowance(userId, amount, metrics, …)`.
3. `ManagedBursarStore.deductWithAllowance()` sends the UsageMetrics to `POST /v1/users/:userId/credits/deduct` (not the amount).
4. Service 2 recalculates from the tenant's authoritative pricing, verifies the result, charges it.

This makes Service 2 the **source of truth** for cost, protecting against:
- Buggy customer-side pricing cache
- Manually tampered SDK
- Drift between cached config and active config

**Impact on `deductWithAllowance` and `createLease`:**
- `deductWithAllowance` receives `(userId, amount, { … metrics … })`. The store sends `{ model, inputTokens, … }` to the service and ignores the SDK's `amount`.
- `createLease` sends the full metrics; server calculates the hold amount from metrics using authoritative pricing.

### Optional optimization: pass metrics alongside amount

To avoid breaking the `CreditStore` contract while still enabling server-side recalculation, the store signature can accept an optional `metrics` bag. The manager already passes `UsageMetrics` down to the store's `deductWithAllowance` in some implementations — verify parity.

**Alternative:** have `ManagedBursarStore.deductWithAllowance` include both `amount` and `metrics` in the request body. The service uses `amount` only if metrics are absent (for backward compat with non-metric deductions like manual adjustments). It always prefers metrics when present.

## Error handling

`ManagedBursarStore` translates Service 2 HTTP errors back into bursar's typed exception classes:

| HTTP | Service 2 `error.type` | bursar exception |
|------|------------------------|-------------------|
| 402 | `insufficient_credits` | `InsufficientCreditsError` |
| 403 | `feature_not_entitled` | `FeatureNotEntitledError` |
| 404 | `lease_not_found` | `LeaseNotFoundError` |
| 409 | `lease_expired` | `LeaseExpiredError` |
| 409 | `pricing_not_loaded` | `PricingNotLoadedError` |
| 429 | `concurrency_limit` | `ConcurrencyLimitError` |
| 429 | `rate_limited` | throttle/sleep and retry |
| 400 | `validation_error` | `CreditError` |
| 401 | `unauthorized` | `CreditError` (the key is wrong — re-check your API key) |
| 403 | `forbidden` | `CreditError` (key doesn't have the required scope) |

This means **customer code using `try/catch` against `InsufficientCreditsError` etc. continues to work unchanged**.

## Pricing config caching

Like `HttpxSupabaseStore`, `ManagedBursarStore` caches the active pricing config for `pricingCacheTtl` seconds (default 300). It fetches from `GET /v1/config/active`.

- On first call (engine load), fetches from service.
- Subsequent calls within TTL use the cached value (no network).
- The `CreditStore` base class already implements a TTL cache (`base.py:101-140` equivalent in JS).

## Static analysis / type safety

The `ManagedBursarStore` class implements the `CreditStore` interface (`credit-store.ts:100`). TypeScript ensures all required methods are implemented. Optional capability methods (analytics, teams, transaction listing) throw `CapabilityNotSupportedError` by default via the abstract class — override those the managed service supports.

## Promises & concurrency

All methods return `Promise<…>` (matching the JS store contract). The underlying `fetch` calls are concurrent-safe — no shared mutable state per request.

## Testing strategy

- Unit tests: mock `fetch` (via injectable `options.fetch`), verify correct URL construction, request body, header handling, and error translation.
- Integration tests: spin up Service 2 (in test mode with `ManagedBursarStore`), run bursar's existing parity test suite with `CreditManager → ManagedBursarStore` against a test tenant.

## Open implementation questions

1. **`deductWithAllowance` protocol:** does the method signature accept a `UsageMetrics` bag alongside the amount? The JS `CreditStore` currently expects `(userId, amount, options?)` — need to confirm `options` can carry metrics for the service to recalculate.
2. **`setActivePricing` / `activatePricing`:** these are admin-only and should raise `CapabilityNotSupportedError` on the data-plane store. The SDK's `publishPricingFromDict` won't work with `ManagedBursarStore` — customers should use Service 1 (UI or admin API) to publish config.
3. **Setup / migrations:** `store.setup()` needs a mechanism for the managed service to run migrations. This could be a no-op in `ManagedBursarStore` (the service handles it), or a `POST /v1/setup` endpoint that triggers tenant initialization.
