# Optimization & Refactor Scope

DRY violations, repeated lookups, dispatch dict rebuilds, and structural simplification opportunities.

## Fix Status

| Finding | Severity | Status |
|---|---|---|
| #17 — `computeTopupCredits` duplicated ×7 | LOW | ✅ Fixed |
| #18 — Dispatch dict rebuilt on every event | LOW | ✅ Fixed |
| #25 — No offer cache | LOW | ✅ Fixed |
| #26 — Stripe retrieves subscription twice | LOW | ✅ Fixed |

**4/4 fixed.**

---

## [LOW] #17 — `computeTopupCredits` formula duplicated across 7 store implementations ✅ Fixed

### Location

| File | Line | Formula |
|---|---|---|
| `javascript/src/billing/memory-billing-store.ts` | 174-180 | `Math.trunc((amountMinor * creditsPer) / 100)` |
| `javascript/src/billing/postgres-billing-store.ts` | 262-268 | `Math.trunc((amountMinor * creditsPer) / 100)` |
| `javascript/src/billing/supabase-billing-store.ts` | 162-168 | `Math.trunc((amountMinor * creditsPer) / 100)` |
| `python/src/bursar/billing/memory.py` | 171-173 | `(amount_minor * credits_per) // 100` |
| `python/src/bursar/billing/postgres.py` | 323-325 | `(amount_minor * credits_per) // 100` |
| `python/src/bursar/billing/supabase.py` | 212-218 | `(amount_minor * credits_per) // 100` |

### Code

Every store implements the same formula independently:

```typescript
// javascript/src/billing/memory-billing-store.ts:174-180
async computeTopupCredits(
  amountMinor: number,
  topupConfig: Record<string, unknown>,
): Promise<number> {
  const creditsPer = (topupConfig.creditsPerMajorUnit as number) ?? 1000;
  return Math.trunc((amountMinor * creditsPer) / 100);
}
```

```python
# python/src/bursar/billing/memory.py:171-173
def compute_topup_credits(self, amount_minor: int, topup_config: dict) -> int:
    credits_per = topup_config.get("credits_per_major_unit", 1000)
    return (amount_minor * credits_per) // 100
```

### Impact

1. **DRY violation**: The same formula is written 7 times. If the formula changes (e.g. adding rounding mode, tax handling), all 7 must be updated in sync.
2. **Key-casing inconsistency**: The Python `SupabaseBillingStore` reads `creditsPerMajorUnit` (camelCase) while the other Python stores read `credits_per_major_unit` (snake_case). This is the root cause of the CRITICAL bug in [02](./02-supabase-store-key-casing-bug.md). Centralizing the formula would eliminate this class of bug.
3. **`computeTopupCredits` doesn't need to be on the store**: The formula is pure math — it doesn't touch the database. It's on the `BillingStore` interface because the original design assumed stores might compute it differently (e.g. SQL-side). In practice, no store overrides the formula.

### Fix

Move the formula to `BillingManager` (or a shared utility) and remove it from the `BillingStore` interface:

```typescript
// javascript/src/billing/billing-manager.ts
private computeTopupCredits(amountMinor: number, topupConfig: Record<string, unknown>): number {
  const creditsPer = (topupConfig.creditsPerMajorUnit as number) ?? 1000;
  return Math.trunc((amountMinor * creditsPer) / 100);
}
```

```python
# python/src/bursar/billing/manager.py
def _compute_topup_credits(self, amount_minor: int, topup_config: dict) -> int:
    credits_per = int(topup_config.get("credits_per_major_unit", 1000))
    return (amount_minor * credits_per) // 100
```

Then remove `computeTopupCredits` / `compute_topup_credits` from the `BillingStore` ABC and all 6 remaining implementations. The `BillingManager` already has the `topupConfig` in hand when it calls `computeTopupCredits` — it doesn't need the store to do the math.

If stores MUST be able to override (e.g. for SQL-side computation), keep it on the interface but provide a default implementation in the base class.

---

## [LOW] #18 — Event handler dispatch dict rebuilt on every `routeEvent` call ✅ Fixed

### Location

- `python/src/bursar/billing/manager.py:71-99` — dict literal inside `_route_event`
- `javascript/src/billing/billing-manager.ts:72-101` — object literal inside `routeEvent`

### Code

```python
# python/src/bursar/billing/manager.py:70-99
def _route_event(self, event: BillingEvent) -> BillingEventResult:
    handlers = {
        "customer.created": self._handle_customer_created,
        "customer.updated": self._handle_customer_updated,
        "customer.deleted": self._handle_customer_deleted,
        "checkout.completed": self._handle_checkout_completed,
        "subscription.created": self._handle_subscription_created,
        # ... 21 entries total
    }
    handler = handlers.get(event.event_type)
    if handler is None:
        logger.debug("unhandled billing event type %s", event.event_type)
        return BillingEventResult(handled=True, action="ignored")
    return handler(event)
```

This dict is constructed on every single `handle_event` call. With 21 entries, that's 21 key-value pairs allocated per webhook.

### Impact

Minor performance overhead — the dict is small and Python/JS GC handles it quickly. But it's wasteful and makes the code harder to read (the handler logic is buried inside a method body). More importantly, it prevents static analysis tools from seeing the handler map.

### Fix

Make the handler map a class-level constant (Python) or a static property (JS):

```python
# python/src/bursar/billing/manager.py
class BillingManager:
    _HANDLERS = None  # Initialized in __init__ to bind methods

    def __init__(self, ...):
        # ...
        self._handlers = {
            "customer.created": self._handle_customer_created,
            "customer.updated": self._handle_customer_updated,
            # ...
        }

    def _route_event(self, event: BillingEvent) -> BillingEventResult:
        handler = self._handlers.get(event.event_type)
        if handler is None:
            logger.debug("unhandled billing event type %s", event.event_type)
            return BillingEventResult(handled=True, action="ignored")
        return handler(event)
```

```typescript
// javascript/src/billing/billing-manager.ts
export class BillingManager {
  private handlerMap: Record<string, (event: BillingEvent) => Promise<BillingEventResult>>;

  constructor(...) {
    this.handlerMap = {
      "customer.created": this.handleCustomerCreated.bind(this),
      "customer.updated": this.handleCustomerUpdated.bind(this),
      // ...
    };
  }

  private async routeEvent(event: BillingEvent): Promise<BillingEventResult> {
    const handler = this.handlerMap[event.eventType];
    if (!handler) {
      return { handled: true, action: "ignored" };
    }
    return handler(event);
  }
}
```

---

## [LOW] #25 — `resolveOfferAndKeys` calls store on every subscription event — no offer cache ✅ Fixed

### Location

`javascript/src/billing/billing-manager.ts:216-220`

### Code

```typescript
// javascript/src/billing/billing-manager.ts:209-237
private async resolveOfferAndKeys(event: BillingEvent): Promise<{...}> {
  const refs = event.subscription?.refs;
  if (!refs) return { offer: null, offerKey: null, planKey: null };
  const offer = await this.store.resolveBillingOffer(  // ← DB call every time
    event.provider,
    refs.productId ?? null,
    refs.priceId ?? null,
  );
  // ...
}
```

Every subscription event (`subscription.created`, `subscription.updated`, `subscription.activated`, `subscription.renewed`, `subscription.plan_changed`, etc.) calls `resolveBillingOffer` which hits the database. For a single webhook that fans out into multiple events (see #5), this means multiple DB calls for the same offer.

### Impact

For a Stripe `checkout.session.completed` webhook that fans into 3 events (`checkout.completed`, `subscription.created`, `subscription.activated`), `resolveOfferAndKeys` is called 2 times (for `subscription.created` and `subscription.activated`). Each call does a DB round-trip. The offer data doesn't change between these calls — it was configured at sync time.

### Fix

Add an in-memory offer cache with TTL:

```typescript
private offerCache = new Map<string, { offer: Record<string, unknown> | null; expires: number }>();
private readonly OFFER_CACHE_TTL_MS = 60_000; // 1 minute

private async resolveBillingOfferCached(
  provider: string,
  productId: string | null,
  priceId: string | null,
): Promise<Record<string, unknown> | null> {
  const cacheKey = `${provider}:${productId ?? ""}:${priceId ?? ""}`;
  const cached = this.offerCache.get(cacheKey);
  if (cached && cached.expires > Date.now()) {
    return cached.offer;
  }
  const offer = await this.store.resolveBillingOffer(provider, productId, priceId);
  this.offerCache.set(cacheKey, { offer, expires: Date.now() + this.OFFER_CACHE_TTL_MS });
  return offer;
}
```

Alternatively, pass the resolved offer through the event fan-out chain so it's resolved once and reused.

---

## [LOW] #26 — Stripe event-mapper retrieves subscription twice in `invoice.paid` flow ✅ Fixed

### Location

`javascript/src/providers/stripe/event-mapper.ts:204-258`

### Code

```typescript
// javascript/src/providers/stripe/event-mapper.ts:204-258
case "invoice.paid": {
  const invoice = event.data.object as Stripe.Invoice;
  const subscriptionId = (invoice as { subscription?: string }).subscription;
  if (!subscriptionId) {
    logger?.debug?.("invoice.paid: no subscription reference", { invoiceId: invoice.id });
    break;
  }

  let stripeSub: Stripe.Subscription;
  try {
    stripeSub = await stripe.subscriptions.retrieve(subscriptionId);  // ← API call 1
  } catch (err) {
    logger?.error?.("invoice.paid: failed to retrieve subscription", { subscriptionId, err });
    break;
  }

  const userId = stripeSub.metadata?.userId;
  if (!userId) {
    logger?.warn?.("invoice.paid: no userId in metadata", { subscriptionId });
    break;
  }

  await bm.handleEvent({
    // ...
    subscription: {
      providerSubscriptionId: subscriptionId,
      status: stripeSub.status as ...,
      periodEnd: buildEnd(stripeSub),  // ← uses stripeSub from API call 1
    },
    invoice: {
      providerInvoiceId: invoice.id,
      status: invoice.status ?? "open",
      amountPaidMinor: invoice.amount_paid,
      amountDueMinor: invoice.amount_due,
      currency: invoice.currency?.toUpperCase() ?? "USD",
    },
  });
  break;
}
```

Then `handleEvent` → `_handle_invoice_paid` → `_handle_subscription_renewed` → `_apply_subscription_event` calls `get_billing_subscription` (another DB call) to get the existing subscription, and `resolveBillingOffer` (another DB call) to resolve the offer.

### Impact

The Stripe `invoice.paid` event triggers:
1. `stripe.subscriptions.retrieve(subscriptionId)` — Stripe API call
2. `bm.handleEvent({ eventType: "invoice.paid", ... })` → `claim_billing_event` — DB call
3. `_handle_invoice_paid` → `upsert_billing_invoice` — DB call
4. `_handle_invoice_paid` → `_handle_subscription_renewed` → `_apply_subscription_event`:
   - `get_billing_subscription` — DB call
   - `resolveBillingOffer` — DB call
   - `upsert_billing_subscription` — DB call
   - `set_user_plan` (via CreditManager) — DB call
5. `complete_billing_event` — DB call

That's 1 Stripe API call + 6 DB calls for a single `invoice.paid` webhook. The Stripe API call and the `get_billing_subscription` DB call are redundant — the subscription data is in the webhook payload (the `invoice` object has a `subscription` field, and the `stripeSub` is already retrieved).

### Fix

The `_handle_subscription_renewed` handler doesn't need to call `get_billing_subscription` if the event already carries the full subscription data. And `resolveBillingOffer` could be skipped if the offer was already resolved earlier in the chain.

For the Stripe API call: if the `invoice` object's `subscription` field is expanded (or if the `lines.data[0].subscription` is available), the `stripe.subscriptions.retrieve` call can be skipped. Use Stripe's `expand` feature:

```typescript
// In the webhook construction, expand the subscription:
const event = stripe.webhooks.constructEvent(rawBody, signature, secret, {
  // Stripe doesn't support expanding webhook events directly,
  // but the invoice.lines.data[0].subscription may be available
});
```

Or accept the API call as necessary (the `invoice` object doesn't always contain the full subscription, so the retrieve may be needed).
