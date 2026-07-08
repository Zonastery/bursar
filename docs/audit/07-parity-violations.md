# Parity Violations: Python ↔ JavaScript

The bursar CLAUDE.md states: "Behavior must match the Python SDK exactly. `MemoryStore` is the reference." This document catalogs every divergence between the Python and JavaScript billing module implementations.

## Fix Status

| Finding | Severity | Status |
|---|---|---|
| #3 — `lookupKey` fallback JS-only | HIGH | ✅ Fixed |
| #27 — `handleEvent` async vs sync | LOW | ❌ Architectural — documented |
| #28 — `ResolveUser` injection point differs | LOW | ❌ Architectural — documented |
| #35 — Offer key casing (camelCase vs snake_case) | LOW | ✅ Fixed (key transformer added to JS supabase store) |

**2/4 fixed.** The remaining 2 (#27 async vs sync, #28 ResolveUser injection) are intentional architectural differences between the two SDKs.

---

## [HIGH] #3 — `lookupKey` fallback exists in JS but NOT Python ✅ Fixed

### Location

- JS: `javascript/src/billing/billing-manager.ts:228-235` — has the fallback
- Python: `python/src/bursar/billing/manager.py:119-132` — does NOT have the fallback

### Code

**JavaScript** (has the fallback):

```typescript
// javascript/src/billing/billing-manager.ts:209-237
private async resolveOfferAndKeys(event: BillingEvent): Promise<{...}> {
  const refs = event.subscription?.refs;
  if (!refs) return { offer: null, offerKey: null, planKey: null };
  const offer = await this.store.resolveBillingOffer(
    event.provider,
    refs.productId ?? null,
    refs.priceId ?? null,
  );
  if (offer) {
    return {
      offer,
      offerKey: (offer?.offerKey as string | null) ?? null,
      planKey: (offer?.planKey as string | null) ?? null,
    };
  }
  // Fallback to lookupKey when no provider refs match (mock/Dodo webhooks)
  if (refs.lookupKey) {
    return {
      offer: { planKey: refs.lookupKey, entitlementMode: "allowance" },
      offerKey: refs.lookupKey,
      planKey: refs.lookupKey,
    };
  }
  return { offer: null, offerKey: null, planKey: null };
}
```

**Python** (no fallback):

```python
# python/src/bursar/billing/manager.py:119-132
def _offer_for_event(self, event: BillingEvent) -> tuple[dict | None, str | None, str | None]:
    if not event.subscription:
        return None, None, None
    refs = event.subscription.refs
    if not refs:
        return None, None, None
    offer = self._store.resolve_billing_offer(
        event.provider,
        product_id=refs.product_id,
        price_id=refs.price_id,
    )
    if not offer:
        return None, None, None  # ← No lookupKey fallback!
    return offer, offer.get("offer_key"), offer.get("plan_key")
```

### Impact

If a Python SaaS uses bursar with Dodo payments (or any provider that sends `refs.lookupKey` without `productId`/`priceId`), the offer resolution returns `(None, None, None)`. The subscription is persisted with `offer_key = None` and `plan_key = None`. `_provision_subscription` reads `plan_key = None` → returns early. **The user's plan is never set.**

This is a direct parity violation. The JS SDK handles Dodo/Mock webhooks correctly via the fallback; the Python SDK does not.

### Fix

Port the `lookupKey` fallback to Python. In `_offer_for_event`:

```python
def _offer_for_event(self, event: BillingEvent) -> tuple[dict | None, str | None, str | None]:
    if not event.subscription:
        return None, None, None
    refs = event.subscription.refs
    if not refs:
        return None, None, None
    offer = self._store.resolve_billing_offer(
        event.provider,
        product_id=refs.product_id,
        price_id=refs.price_id,
    )
    if offer:
        return offer, offer.get("offer_key"), offer.get("plan_key")
    # Fallback to lookupKey when no provider refs match (matches JS SDK)
    if refs.lookup_key:
        return (
            {"plan_key": refs.lookup_key, "entitlement_mode": "allowance"},
            refs.lookup_key,
            refs.lookup_key,
        )
    return None, None, None
```

Note: The Python `BillingProviderRefs` model uses `lookup_key` (snake_case), not `lookupKey` (camelCase).

---

## [LOW] #27 — JS `handleEvent` is `async`; Python `handle_event` is synchronous ❌ Architectural

### Location

- JS: `javascript/src/billing/billing-manager.ts:51` — `async handleEvent(event: BillingEvent): Promise<BillingEventResult>`
- Python: `python/src/bursar/billing/manager.py:51` — `def handle_event(self, event: BillingEvent) -> BillingEventResult`

### Code

```typescript
// javascript — async
async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
  const claim = await this.store.claimBillingEvent(...);
  // ...
  const result = await this.routeEvent(event);
  await this.store.completeBillingEvent(...);
  return result;
}
```

```python
# python — sync
def handle_event(self, event: BillingEvent) -> BillingEventResult:
    claim = self._store.claim_billing_event(...)
    # ...
    result = self._route_event(event)
    self._store.complete_billing_event(...)
    return result
```

### Impact

This is a fundamental architectural difference, not a bug:

- **JS**: All store methods are `async` (return `Promise`). This is required because the JS `PostgresStore` uses `pg` (callback-based pool), `HttpxSupabaseStore` uses `fetch` (async), and `MemoryStore` is async for interface consistency. The `BillingManager.handleEvent` must `await` each store call.

- **Python**: The `MemoryStore` and `PostgresStore` (via `psycopg2`) are synchronous. The `SupabaseStore` uses the `supabase-py` client which is also synchronous (uses `httpx` sync mode). So `handle_event` is synchronous.

The difference affects:
1. **Concurrency**: JS `handleEvent` can be interleaved with other async operations. Python `handle_event` blocks the event loop (or thread). In an async Python framework (FastAPI, asyncio), the caller must run `handle_event` in a thread executor.
2. **Error handling**: JS `try/catch` catches both sync throws and promise rejections. Python `try/except` catches synchronous exceptions only.
3. **Testing**: JS tests use `async/await`; Python tests are synchronous.

### Fix

This is a known architectural difference, not a fixable parity violation. Document it:

- The JS SDK is async-first (all public methods return `Promise`).
- The Python SDK is sync-first (all public methods are synchronous).
- A Python SaaS using an async framework (FastAPI) must run `handle_event` in a thread executor: `await asyncio.to_thread(bm.handle_event, event)`.

If async Python is desired in the future, add an `AsyncBillingManager` that wraps the sync one, or make the stores async using `asyncpg` and `httpx.AsyncClient`.

---

## [LOW] #28 — `ResolveUserCallback` injection point differs between SDKs ❌ Architectural

### Location

- JS: `javascript/src/providers/types.ts:51-54` — `ResolveUserCallback` on the provider
- Python: `python/src/bursar/billing/manager.py:21` — `ResolveUserFn` on `BillingManager`

### Code

**JavaScript** — `ResolveUserCallback` is a provider-level concern:

```typescript
// javascript/src/providers/types.ts:51-54
export type ResolveUserCallback = (
  data: Record<string, unknown>,
  metadata: Record<string, string>,
) => Promise<string | null>;
```

```typescript
// javascript/src/providers/dodo/provider.ts:18-24
constructor(
  private getClient: () => DodoPayments,
  private config: { webhookKey: string; setupProductId?: string },
  private bm: BillingManager,
  private resolveUser?: ResolveUserCallback,  // ← on the provider
  private logger?: ProviderLogger,
) {}
```

The provider calls `resolveUser` during `handleWebhook` to resolve the user ID from webhook payload, then passes the resolved `userId` to the event-mapper, which includes it in the `BillingEvent`.

**Python** — `ResolveUserFn` is a `BillingManager`-level concern:

```python
# python/src/bursar/billing/manager.py:21
ResolveUserFn = Callable[[str, str | None, str | None], str | None]
```

```python
# python/src/bursar/billing/manager.py:30-43
class BillingManager:
    def __init__(
        self,
        billing_store: BillingStore,
        credit_manager: CreditManager | None = None,
        emitter: CreditEventEmitter | None = None,
        resolve_user: ResolveUserFn | None = None,  # ← on the manager
        config: BillingConfig | None = None,
    ) -> None:
```

The `BillingManager` calls `resolve_user` inside `_resolve_user_id` when `event.user_id` is not set and a `provider_customer_id` is available.

### Impact

The two SDKs resolve users at different layers:

- **JS**: User resolution happens in the **provider** (before the event reaches `BillingManager`). The `BillingEvent` already has `userId` set when `handleEvent` is called.
- **Python**: User resolution happens in the **BillingManager** (during event processing). The `BillingEvent` may not have `userId` set; the manager resolves it from `provider_customer_id`.

This means:
1. A JS SaaS passes `resolveUser` to the provider constructor. A Python SaaS passes `resolve_user` to the `BillingManager` constructor. Different wiring.
2. The JS `ResolveUserCallback` receives `(data, metadata)` — raw webhook payload. The Python `ResolveUserFn` receives `(provider, provider_customer_id, email)` — already-extracted fields. Different signatures.
3. In JS, if the provider can't resolve the user, the event-mapper sets `userId: null` and the `BillingManager` receives an event with no user. In Python, the `BillingManager` tries `resolve_user` as a fallback. This means the Python SDK has an extra resolution attempt that JS doesn't.

### Fix

Align the injection points. Two options:

**Option A — Move to BillingManager in JS (match Python):**

Add `resolveUser` to the JS `BillingManager` constructor and call it in `resolveUserId` when `event.userId` is null:

```typescript
// javascript/src/billing/billing-manager.ts
constructor(
  store: BillingStore,
  options?: {
    creditManager?: CreditManager;
    resolveUser?: (provider: string, providerCustomerId?: string, email?: string) => Promise<string | null>;
    // ...
  }
) {}

private async resolveUserId(event: BillingEvent): Promise<string | null> {
  if (event.userId) return event.userId;
  if (event.customer?.providerCustomerId) {
    const uid = await this.store.getBillingCustomer(event.provider, event.customer.providerCustomerId);
    if (uid) return uid;
  }
  if (this.resolveUser && event.customer) {
    return this.resolveUser(event.provider, event.customer.providerCustomerId, event.customer.email);
  }
  return null;
}
```

**Option B — Keep both injection points (document the difference):**

The current design has a valid rationale: in JS, the provider has access to the raw webhook payload (needed for user resolution via email lookup, magic link, etc.), while in Python the provider interface is minimal and the manager handles resolution. Document this as an intentional architectural difference, not a parity violation.

---

## [LOW] #35 — Offer key casing: Python reads snake_case, JS reads camelCase ✅ Fixed

### Location

- Python: `python/src/bursar/billing/manager.py:132` — `offer.get("offer_key")` (snake_case)
- JS: `javascript/src/billing/billing-manager.ts:224` — `offer.offerKey` (camelCase)

### Code

**Python** — reads snake_case from offer dict (matching Postgres JSONB output):

```python
# python/src/bursar/billing/manager.py:130-132
if not offer:
    return None, None, None
return offer, offer.get("offer_key"), offer.get("plan_key")
```

```python
# python/src/bursar/billing/manager.py:562
plan_key = offer.get("plan_key")
```

**JavaScript** — reads camelCase from offer object (matching MemoryStore, but NOT matching raw Supabase RPC output):

```typescript
// javascript/src/billing/billing-manager.ts:224
offerKey: (offer?.offerKey as string | null) ?? null,
planKey: (offer?.planKey as string | null) ?? null,
```

```typescript
// javascript/src/billing/billing-manager.ts:647
const planKey = offer.planKey as string | undefined;
```

### Impact

This is correct for each SDK's ecosystem:
- Python stores return dicts with snake_case keys (matching Postgres JSONB and Python conventions).
- JS `MemoryStore` and `PostgresStore` return objects with camelCase keys (matching JS conventions).

The parity violation is in the **SupabaseStore** — the JS `SupabaseBillingStore` returns snake_case keys (raw RPC output) while the JS `PostgresBillingStore` returns camelCase keys (via `snakeToCamelKeys`). The Python `SupabaseBillingStore` returns snake_case, which is correct for Python. See [02](./02-supabase-store-key-casing-bug.md) for the full analysis.

### Fix

No change needed to the managers — they're each correct for their ecosystem. The fix is in the JS `SupabaseBillingStore`: add `snakeToCamelKeys` transformation (as described in [02](./02-supabase-store-key-casing-bug.md)) so all JS stores consistently return camelCase.

---

## Summary Table

| # | Finding | JS | Python | Severity |
|---|---|---|---|---|
| 3 | `lookupKey` fallback | Has (`billing-manager.ts:228-235`) | Missing (`manager.py:119-132`) | HIGH |
| 27 | `handleEvent` async vs sync | `async` | sync | LOW (architectural) |
| 28 | `ResolveUserCallback` injection point | On provider (`providers/types.ts:51`) | On manager (`manager.py:21`) | LOW (architectural) |
| 35 | Offer key casing | camelCase (`offer.offerKey`) | snake_case (`offer.get("offer_key")`) | LOW (correct per ecosystem) |

### Additional parity items verified as consistent

These were checked and found to match between SDKs:

| Item | JS | Python | Status |
|---|---|---|---|
| `BillingEventType` enum | 33 types (`billing-types.ts:10-45`) | 33 types (`models.py:14-49`) | Match |
| `BillingSubscriptionStatus` enum | 9 states (`billing-types.ts:47-56`) | 9 states (`models.py:52-61`) | Match |
| Handler map | 22 entries (`billing-manager.ts:73-96`) | 21 entries (`manager.py:71-94`) | JS has 1 extra (`subscription.resumed` handler) — verify |
| `BillingStore` abstract methods | 16 methods (`billing-store.ts`) | 16 methods (`store.py`) | Match |
| `BillingConfig` model | `BillingOffer` + `BillingCreditTopup` (`billing-types.ts:195-220`) | `BillingOffer` + `BillingCreditTopup` (`models.py:163-193`) | Match |
| `claim_billing_event` idempotency | claim → complete/fail (`billing-manager.ts:51-70`) | claim → complete/fail (`manager.py:51-68`) | Match (but both lack retry — see #6) |
| `computeTopupCredits` formula | `(amountMinor * creditsPer) / 100` | `(amount_minor * credits_per) // 100` | Match (but duplicated — see #17) |
| Refund clawback logic | `billing-manager.ts:589-605` | `manager.py:509-521` | Match (but both have default-1000 bug — see #15) |
| SQL migrations | `013_billing.sql`, `014_cycle_grant.sql`, `015_billing_records.sql` | Same files | Match (shared SQL) |
