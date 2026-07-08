# Hacks & Workarounds

Patterns that work but are fragile, leaky abstractions, or paper over deeper design issues. Each entry documents the hack, why it exists, and what a proper fix looks like.

## Fix Status

| Finding | Severity | Status |
|---|---|---|
| #11 — `lookupKey` fallback synthesizes fake offer | MEDIUM | ✅ Fixed (added to Python SDK for parity) |
| #19 — Synthetic event-ID suffixing | LOW | ✅ Fixed (obsoleted by #5 consolidation) |
| #20 — `JSON.parse(JSON.stringify(config))` clone hack | LOW | ✅ Fixed |
| #21 — `_coalesce` merge helper | LOW | ✅ Fixed |
| #22 — Dodo PM update via checkout | LOW | ❌ Dodo SDK limitation |
| #23 — `claimBillingEvent` returns `retry` with no consumer | LOW | ✅ Fixed |

**5/6 fixed.** The remaining 1 is a Dodo SDK limitation — no native "update payment method" API exists.

---

## [MEDIUM] #11 — `lookupKey` fallback synthesizes fake offer from metadata string ✅ Fixed (added to Python SDK for parity)

### Location

`javascript/src/billing/billing-manager.ts:228-235`

### Code

```typescript
// javascript/src/billing/billing-manager.ts:216-237
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
    //       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    //       Synthetic offer — NOT from the billing_offers table.
    //       Hardcodes entitlementMode to "allowance".
    //       No cycle_grant_credits, no interval, no offer_key.
    offerKey: refs.lookupKey,
    planKey: refs.lookupKey,
    //       lookupKey is used as both offerKey and planKey.
    //       A metadata string is being treated as a plan key.
  };
}
return { offer: null, offerKey: null, planKey: null };
```

### Why it exists

Dodo and Mock webhooks carry `metadata.plan_slug` (set at checkout creation time by the web layer). The Dodo event-mapper maps this to `refs.lookupKey`:

```typescript
// javascript/src/providers/dodo/event-mapper.ts:48
refs: metadata.plan_slug ? { lookupKey: metadata.plan_slug } : undefined,
```

The `billing_provider_refs` table may not have a row for this Dodo product/price ID (if the SaaS hasn't configured `providerRefs` in their `BillingConfig`). The `lookupKey` fallback lets the subscription resolve to a plan purely from the `plan_slug` metadata string, bypassing the offer resolution entirely.

### Impact

1. **`entitlementMode` is hardcoded to `"allowance"`**: If the offer is configured with `entitlementMode: "cycle_grant"`, the fallback ignores it. Cycle grants are never issued when the fallback is used.

2. **`planKey = lookupKey`**: The `lookupKey` (a metadata string like `"pro_monthly"`) is used as both the `offerKey` and the `planKey`. This assumes the `plan_slug` metadata matches the `plan_key` in the `credit_plans` table exactly. If they differ (e.g. plan key is `"pro"`, slug is `"pro_monthly"`), `setUserPlan` will fail with `plan_not_found`.

3. **No validation**: The fallback doesn't check if the plan key actually exists in the `credit_plans` table. It just passes the string through. `CreditManager.setUserPlan` will fail silently (returns `{ error: "plan_not_found" }`).

4. **Bypasses the offer table**: The entire `billing_offers` → `billing_provider_refs` → `resolve_billing_offer_by_price` resolution chain is skipped. This means the subscription row is persisted with `offer_key = lookupKey` (a metadata string) instead of an actual offer key.

5. **Masks the supabase store bug**: The CRITICAL bug in [02](./02-supabase-store-key-casing-bug.md) would be immediately visible if not for this fallback. The fallback kicks in because `resolveBillingOffer` returns `null` (due to the key-casing mismatch), and the fallback silently "fixes" it by using the metadata string instead. This means Stripe webhooks that happen to carry `plan_slug` metadata appear to work, while those that don't fail silently.

### Fix

**Short-term — Look up the offer by `lookupKey` in the store:**

Add a `resolveBillingOfferByLookupKey` method to `BillingStore` that queries `billing_provider_refs` by `lookup_key` instead of `price_id`/`product_id`:

```sql
CREATE OR REPLACE FUNCTION public.resolve_billing_offer_by_lookup(
    p_provider TEXT,
    p_lookup_key TEXT
)
RETURNS JSONB
-- ... same pattern as resolve_billing_offer_by_price
-- SELECT * FROM billing_provider_refs
-- WHERE provider = p_provider AND lookup_key = p_lookup_key AND resource_type = 'offer'
```

```typescript
// In resolveOfferAndKeys:
if (!offer && refs.lookupKey) {
  const lookupOffer = await this.store.resolveBillingOfferByLookup(event.provider, refs.lookupKey);
  if (lookupOffer) {
    return { offer: lookupOffer, offerKey: lookupOffer.offerKey, planKey: lookupOffer.planKey };
  }
}
// Only use the raw planKey fallback if no offer is found at all
if (refs.lookupKey) {
  // At least validate the plan exists
  return {
    offer: { planKey: refs.lookupKey, entitlementMode: "allowance" },
    offerKey: refs.lookupKey,
    planKey: refs.lookupKey,
  };
}
```

**Long-term — Require `providerRefs` in config**: The `BillingConfig` should require that every offer with a Dodo provider has `providerRefs.dodo.lookupKey` set. The `syncBillingFromConfig` RPC already writes `lookup_key` to `billing_provider_refs`. The fallback should only be needed if the SaaS hasn't synced their config — which is a config error, not a runtime fallback.

---

## [LOW] #19 — Synthetic event-ID suffixing (`${event.id}_checkout`, `${event.id}_sub`, etc.) ✅ Fixed

### Location

- `javascript/src/providers/stripe/event-mapper.ts:52, 70, 97`
- `javascript/src/providers/dodo/event-mapper.ts:42, 54`

### Code

```typescript
// Stripe event-mapper: one Stripe event → 3 bursar events with suffixed IDs
await bm.handleEvent({
  eventId: `${event.id}_checkout`,        // ← evt_123_checkout
  eventType: "checkout.completed",
  // ...
});
await bm.handleEvent({
  eventId: `${event.id}_sub`,             // ← evt_123_sub
  eventType: "subscription.created",
  // ...
});
await bm.handleEvent({
  eventId: `${event.id}_sub_activated`,   // ← evt_123_sub_activated
  eventType: "subscription.activated",
  // ...
});
```

```typescript
// Dodo event-mapper: same pattern
await bm.handleEvent({ eventId: `${rawId}_created`, ... });
await bm.handleEvent({ eventId: `${rawId}_activated`, ... });
```

### Why it exists

The `billing_events` table has a unique constraint on `(provider, provider_event_id)`. If the event-mapper emitted all 3 events with the same `event.id`, the second `claim_billing_event` call would return `duplicate` and be skipped. The suffixes ensure each fan-out event has a distinct `eventId` for idempotency.

### Impact

1. **Fragile**: The suffix pattern is a string convention, not a typed guarantee. If two different Stripe events happen to have IDs that collide after suffixing (e.g. `evt_123` and `evt_123_sub` both produce `evt_123_sub` as a `subscription.created` eventId), the second one is silently dropped as a duplicate.

2. **Not documented**: The suffix convention isn't documented anywhere. A developer reading the `billing_events` table would see `evt_123_sub_activated` and not know it came from `evt_123`.

3. **Tied to #5 (non-atomic fan-out)**: The suffixes exist because of the multi-event fan-out pattern. If the fan-out is replaced with a single event (as proposed in #5), the suffixes become unnecessary.

4. **Provider-specific**: Stripe uses `_checkout` / `_sub` / `_sub_activated`. Dodo uses `_created` / `_activated`. There's no shared convention. A SaaS adding a new provider has to invent their own suffix scheme.

### Fix

If the multi-event fan-out is kept (not replaced by #5), formalize the suffix convention:

```typescript
// javascript/src/providers/types.ts
export const EVENT_ID_SUFFIXES = {
  checkout: "_checkout",
  subscriptionCreated: "_sub_created",
  subscriptionActivated: "_sub_activated",
  payment: "_payment",
} as const;
```

Or use a deterministic hash: `eventId = hash(provider + event.id + eventType)` — guarantees uniqueness without string conventions.

If the fan-out is replaced with a single event (recommended in #5), the suffixes are no longer needed — the original `event.id` is used directly.

---

## [LOW] #20 — `JSON.parse(JSON.stringify(config))` deep-clone hack ✅ Fixed

### Location

`javascript/src/billing/supabase-billing-store.ts:19`

### Code

```typescript
// javascript/src/billing/supabase-billing-store.ts:17-22
async syncBillingFromConfig(config: BillingConfig): Promise<void> {
  const { error } = await this.supabase.rpc("sync_billing_from_config", {
    p_config: JSON.parse(JSON.stringify(config)),
    //          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    //          Deep clone to plain JSON — strips undefined values,
    //          class instances, functions, etc.
  });
  if (error) throw error;
}
```

### Why it exists

The supabase-js client serializes RPC parameters as JSON. If the `BillingConfig` object contains `undefined` values (which TypeScript allows for optional fields), the Postgres JSONB column will store them as `null` instead of omitting them. The `JSON.parse(JSON.stringify(...))` trick strips `undefined` values — `JSON.stringify` omits them, and `JSON.parse` produces a clean plain object.

### Impact

1. **Performance**: Deep-clones the entire config on every `syncBillingFromConfig` call. For large configs with many offers, this is wasteful.
2. **Data loss**: `JSON.stringify` drops `undefined` values silently. If an offer intentionally has `cycleGrantCredits: undefined` to mean "not set", it's omitted from the JSONB. This is actually the desired behavior, but it's implicit.
3. **Non-obvious**: A developer reading this code may not understand why the clone is needed and may remove it, introducing a subtle bug.

### Fix

Use `structuredClone` (available in Node.js 17+ and all modern browsers) which handles `undefined` correctly, or use a targeted `omitUndefined` utility:

```typescript
// Option A: structuredClone (preserves undefined, which JSONB handles as null)
p_config: structuredClone(config),

// Option B: explicit undefined stripping
function stripUndefined<T>(obj: T): T {
  if (obj && typeof obj === "object") {
    for (const key of Object.keys(obj)) {
      if ((obj as Record<string, unknown>)[key] === undefined) {
        delete (obj as Record<string, unknown>)[key];
      } else {
        stripUndefined((obj as Record<string, unknown>)[key]);
      }
    }
  }
  return obj;
}

p_config: stripUndefined({ ...config }),
```

The postgres store uses `this.camelToSnake(config)` + `JSON.stringify()` which also strips undefined — so the hack is consistent with the other store, just less explicit.

---

## [LOW] #21 — `_coalesce` helper papering over inconsistent subscription state data ✅ Fixed

### Location

`python/src/bursar/billing/manager.py:24-26, 152-175`

### Code

```python
# python/src/bursar/billing/manager.py:24-26
def _coalesce(*values: Any, default: Any = None) -> Any:
    """Return the first non-None value, or `default`."""
    return next((v for v in values if v is not None), default)
```

Used in `_subscription_state` to merge event data over existing state:

```python
# python/src/bursar/billing/manager.py:148-175
return BillingSubscriptionState(
    user_id=uid,
    provider=event.provider,
    provider_subscription_id=sub.provider_subscription_id,
    provider_customer_id=_coalesce(
        event.customer.provider_customer_id if event.customer else None,
        existing.provider_customer_id if existing else None,
    ),
    offer_key=_coalesce(offer_key, existing.offer_key if existing else None),
    plan_key=_coalesce(plan_key, existing.plan_key if existing else None),
    status=_coalesce(
        status,
        sub.status.value if sub.status else None,
        existing.status if existing else None,
        "incomplete",
    ),
    current_period_start=_coalesce(sub.period_start, existing.current_period_start if existing else None),
    current_period_end=_coalesce(sub.period_end, existing.current_period_end if existing else None),
    cancel_at_period_end=_coalesce(
        cancel_at_period_end,
        sub.cancel_at_period_end,
        existing.cancel_at_period_end if existing else None,
        False,
    ),
    # ...
)
```

### Why it exists

Subscription events arrive with partial data. A `subscription.cancellation_scheduled` event carries `cancel_at_period_end: true` but may not include `status` or `period_end`. The `_coalesce` helper merges the event's partial data over the existing subscription state, falling back to defaults. This is a 3-way merge: explicit handler override → event data → existing DB state → hardcoded default.

### Impact

1. **Implicit precedence**: The precedence chain (override → event → existing → default) is encoded in the argument order of each `_coalesce` call. There's no type safety ensuring the right precedence — it's easy to swap arguments and introduce a bug.

2. **`None` is overloaded**: `None` means both "not provided in this event" and "explicitly set to null by the provider". `_coalesce` can't distinguish them. If a provider sends `cancel_at_period_end: null` (meaning "unknown"), `_coalesce` skips it and falls back to the existing value — which may be wrong.

3. **The JS equivalent is more verbose**: The JS `buildSubscriptionState` uses `??` chains:

```typescript
cancelAtPeriodEnd: cancelAtPeriodEnd ?? event.subscription?.cancelAtPeriodEnd ?? existing?.cancelAtPeriodEnd ?? false,
```

This is functionally equivalent but even harder to read and verify.

### Fix

Define an explicit merge strategy with typed intent:

```python
@dataclass
class SubscriptionStateMerge:
    """Explicit merge of event data over existing state."""
    explicit_overrides: dict[str, Any]  # values set by the handler (e.g. status="canceled")
    event_data: BillingSubscriptionInfo  # raw event subscription data
    existing: BillingSubscriptionState | None  # current DB state

    def resolve(self, field: str, default: Any = None) -> Any:
        if field in self.explicit_overrides:
            return self.explicit_overrides[field]
        event_val = getattr(self.event_data, field, None) if self.event_data else None
        if event_val is not None:
            return event_val
        if self.existing:
            return getattr(self.existing, field, default)
        return default
```

This makes the precedence explicit and type-safe, and allows distinguishing "not provided" from "explicitly null".

---

## [LOW] #22 — Dodo payment-method-update via checkout session creation ❌ Not fixed

### Location

`javascript/src/providers/dodo/provider.ts:87-101`

### Code

```typescript
// javascript/src/providers/dodo/provider.ts:87-101
async createUpdatePaymentMethodSession(
  params: UpdatePaymentMethodParams,
): Promise<{ url: string }> {
  const productId = params.productId ?? this.config.setupProductId;
  if (!productId) throw new Error("productId is required for payment method update");
  const client = this.getClient();
  const response = await client.checkoutSessions.create({
    product_cart: [{ product_id: productId, quantity: 1 }],
    customer: { customer_id: params.customerId },
    return_url: params.returnUrl,
    metadata: { purpose: "update_payment_method", subscription_id: params.subscriptionId },
  });
  if (!response.checkout_url) throw new Error("Failed to create payment method update session");
  return { url: response.checkout_url };
}
```

### Why it exists

Dodo Payments doesn't have a native "update payment method" API (no equivalent to Stripe's `billingPortal.sessions.create` with `flow_data: { type: "payment_method_update" }`). The workaround creates a new checkout session with a dummy product and tags it with `metadata.purpose: "update_payment_method"`. The user goes through a checkout flow that effectively updates their payment method.

### Impact

1. **Creates a payment record**: The checkout session creates a payment in Dodo, even though no actual purchase is intended. This may generate a `payment.succeeded` webhook, which bursar would process as a credit topup (if the product is configured as a topup). The `metadata.purpose` field is not checked by `handlePaymentSucceeded` — it only checks `event.payment.purpose`, which the event-mapper sets based on `data.subscription_id` presence.

2. **Requires a dummy product**: The `setupProductId` config is a workaround — the SaaS must create a $0 (or minimal) product in Dodo just for payment method updates.

3. **Poor UX**: The user sees a checkout page instead of a "update your card" form.

### Fix

This is a Dodo SDK limitation. Document it clearly:

```typescript
/**
 * Dodo does not have a native "update payment method" API.
 * This method creates a checkout session with a dummy product
 * (configured via `setupProductId`) as a workaround.
 *
 * WARNING: This may trigger a `payment.succeeded` webhook.
 * Ensure the `setupProductId` product is NOT configured as a
 * `credit_topup` in BillingConfig, or the user will receive
 * spurious credits.
 */
async createUpdatePaymentMethodSession(...): Promise<{ url: string }> {
  // ...
}
```

Additionally, the `handlePaymentSucceeded` handler should check `metadata.purpose` and skip credit grants for `update_payment_method` / `setup_payment_method` purposes:

```typescript
// In handlePaymentSucceeded, before granting credits:
if (event.metadata?.purpose === "update_payment_method" || event.metadata?.purpose === "setup_payment_method") {
  return { handled: true, action: "payment_succeeded_skipped" };
}
```

---

## [LOW] #23 — `claimBillingEvent` defensive `{status: "retry"}` fallback with no retry consumer ✅ Fixed

### Location

`javascript/src/billing/supabase-billing-store.ts:49`

### Code

```typescript
// javascript/src/billing/supabase-billing-store.ts:38-55
async claimBillingEvent(...): Promise<BillingEventClaim> {
  const { data, error } = await this.supabase.rpc("claim_billing_event", {...});
  if (error || !data) return { status: "retry" };
  // ← Returns "retry" on ANY DB error, but nothing in the system
  //   ever retries a "retry" status. handleEvent falls through to
  //   routeEvent without a claim, and completeBillingEvent/failBillingEvent
  //   try to update a non-existent row.
  const result = data as Record<string, string>;
  const s = result.status;
  if (s === "claimed") return { status: "claimed" as const };
  if (s === "duplicate") return { status: "duplicate" as const };
  return { status: "retry" as const };
}
```

### Why it exists

The `claim_billing_event` RPC can fail for transient reasons (DB connection timeout, Supabase rate limit). Returning `{ status: "retry" }` instead of throwing is a defensive choice — the intent is "this isn't a permanent failure, try again later."

### Impact

The `retry` status is returned to `handleEvent`, which doesn't handle it (see #6 in [03](./03-error-prone-code.md)):

```typescript
async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
  const claim = await this.store.claimBillingEvent(...);
  if (claim.status === "duplicate") return { handled: true, action: "duplicate" };
  // ← No check for claim.status === "retry"!
  try {
    const result = await this.routeEvent(event);  // ← runs without a claim
    await this.store.completeBillingEvent(...);    // ← tries to update non-existent row
    return result;
  } catch (err) {
    await this.store.failBillingEvent(...);        // ← tries to update non-existent row
    return { handled: false, error: String(err) };
  }
}
```

The event is processed without being claimed. If `completeBillingEvent` or `failBillingEvent` fail silently (the RPC updates 0 rows, which is not an error in Supabase), the event is effectively lost. If the provider resends the webhook, `claimBillingEvent` may succeed this time — but the event was already partially processed.

### Fix

Handle `retry` in `handleEvent`:

```typescript
async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
  const claim = await this.store.claimBillingEvent(event.provider, event.eventId, event.eventType);
  if (claim.status === "duplicate") return { handled: true, action: "duplicate" };
  if (claim.status === "retry") {
    // Claim failed — don't process the event. The caller (webhook handler)
    // should return a non-200 status so the provider retries.
    return { handled: false, error: "claim_failed_retry" };
  }
  // ... existing try/catch for "claimed"
}
```

The webhook route handler should then return a 500 (retryable) status:

```typescript
// web/app/api/payments/webhook/route.ts
const result = await provider.handleWebhook(request);
if (result.error === "claim_failed_retry") {
  return new Response("retry", { status: 500 });
}
return new Response(null, { status: result.received ? 200 : 400 });
```
