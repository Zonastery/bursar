# Error-Prone Code

Non-atomic multi-event fan-out, silent error swallowing, missing retry mechanisms, unguarded parsing, and incorrect fallback defaults.

## Fix Status

| Finding | Severity | Status |
|---|---|---|
| #5 — Stripe non-atomic fan-out | HIGH | ✅ Fixed |
| #6 — No retry sweep for stuck events | HIGH | ✅ Fixed |
| #13 — `datetime.fromisoformat` no try/except | MEDIUM | ✅ Fixed |
| #15 — Refund clawback default 1000 | MEDIUM | ✅ Fixed |
| #16 — JS silent catch | MEDIUM | ✅ Fixed |

**5/5 fixed.**

---

## [HIGH] #5 — Stripe event-mapper: non-atomic multi-event fan-out ✅ Fixed

### Location

`javascript/src/providers/stripe/event-mapper.ts:49-139`

### Code

The `checkout.session.completed` handler fans out into up to 3 independent `bm.handleEvent()` calls:

```typescript
// javascript/src/providers/stripe/event-mapper.ts:49-56  — Event 1: checkout.completed
await bm.handleEvent({
  provider: "stripe",
  eventId: `${event.id}_checkout`,
  eventType: "checkout.completed",
  occurredAt: new Date().toISOString(),
  userId,
  customer: customerInfo,
});

// javascript/src/providers/stripe/event-mapper.ts:68-91  — Event 2: subscription.created
await bm.handleEvent({
  provider: "stripe",
  eventId: `${event.id}_sub`,
  eventType: "subscription.created",
  // ...
});

// javascript/src/providers/stripe/event-mapper.ts:94-106  — Event 3: subscription.activated
if (sub.status === "active" || sub.status === "trialing") {
  await bm.handleEvent({
    provider: "stripe",
    eventId: `${event.id}_sub_activated`,
    eventType: "subscription.activated",
    // ...
  });
}
```

Each `handleEvent` call independently claims, processes, and completes its own `billing_events` row. If event 1 succeeds but event 2 throws (e.g. DB error during `upsertBillingSubscription`), event 1 is already committed — there is no rollback.

### Impact

**Partial state scenario**: A Stripe checkout webhook arrives. `checkout.completed` is processed and committed (customer record upserted). Then `subscription.created` fails (DB connection drops). The result:
- `billing_customers` row exists (from event 1)
- `billing_subscriptions` row does NOT exist (event 2 failed)
- `billing_events` shows event 1 as `completed`, event 2 as `failed`
- The user's plan is never set (no `subscription.activated` event)
- Stripe will NOT resend the webhook (it received a 200 from event 1's processing)

The user is in a limbo state: the provider has a subscription, bursar has a customer, but there's no subscription record and no plan.

### Fix

**Option A — Single transaction**: Wrap all three events in a single DB transaction. This requires `BillingManager` to accept a transaction context, which is a significant refactor.

**Option B — Sequential with rollback**: If event 2 fails, attempt to undo event 1's effects (delete the customer record). Fragile and not recommended.

**Option C — Single normalized event (recommended)**: Instead of fanning out into 3 events, emit a single `checkout.completed` event that carries all the subscription data. The `BillingManager` handler for `checkout.completed` then creates the subscription AND activates it in one atomic `handleEvent` call.

```typescript
// Proposed: single event with full context
await bm.handleEvent({
  provider: "stripe",
  eventId: event.id,  // ← single event ID, not suffixed
  eventType: "checkout.completed",
  occurredAt: new Date().toISOString(),
  userId,
  customer: customerInfo,
  subscription: session.mode === "subscription" ? {
    providerSubscriptionId: subId,
    status: sub.status,
    cancelAtPeriodEnd: sub.cancel_at_period_end,
    periodEnd: end,
    refs: planSlug ? { lookupKey: planSlug } : undefined,
  } : undefined,
  payment: session.mode !== "subscription" ? paymentInfo : undefined,
});
```

Then the `handleCheckoutCompleted` handler in `BillingManager` does both customer upsert AND subscription upsert AND provisioning in one atomic claim→route→complete cycle.

---

## [HIGH] #6 — No retry sweep for `processing` / `retry` billing events ✅ Fixed

### Location

- `javascript/src/billing/billing-manager.ts:51-70` — `handleEvent` claim/complete/fail lifecycle
- `javascript/src/billing/supabase-billing-store.ts:49` — `claimBillingEvent` returns `{ status: "retry" }` on DB error
- `python/src/bursar/billing/manager.py:51-68` — Python equivalent

### Code

```typescript
// javascript/src/billing/billing-manager.ts:51-70
async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
  const claim = await this.store.claimBillingEvent(event.provider, event.eventId, event.eventType);
  if (claim.status === "duplicate") return { handled: true, action: "duplicate" };
  try {
    const result = await this.routeEvent(event);
    await this.store.completeBillingEvent(event.provider, event.eventId);
    return result;
  } catch (err) {
    await this.store.failBillingEvent(event.provider, event.eventId);
    return { handled: false, error: String(err) };
  }
}
```

```typescript
// javascript/src/billing/supabase-billing-store.ts:38-55
async claimBillingEvent(...): Promise<BillingEventClaim> {
  const { data, error } = await this.supabase.rpc("claim_billing_event", {...});
  if (error || !data) return { status: "retry" };  // ← DB error → "retry"
  // ...
}
```

The `claim_billing_event` SQL RPC creates a row with `status = 'processing'`. On success, `complete_billing_event` sets it to `'completed'`. On failure, `fail_billing_event` sets it to `'failed'`.

### Impact

Three stuck-state scenarios with no recovery:

1. **Crash during processing**: The process crashes between `claimBillingEvent` and `completeBillingEvent`. The `billing_events` row stays `processing` forever. If the provider resends the webhook, `claim_billing_event` sees the existing row and returns `{ status: "duplicate" }` — but the row is `processing`, not `completed`. The duplicate check doesn't distinguish `processing` from `completed`. **The event is permanently stuck.**

2. **DB error during claim**: `claimBillingEvent` returns `{ status: "retry" }`. But `handleEvent` doesn't handle `retry` — it falls through to the `try` block, which calls `routeEvent` without a claim. If routing succeeds, `completeBillingEvent` tries to update a row that doesn't exist. If routing fails, `failBillingEvent` tries to update a row that doesn't exist. **Both are no-ops on a non-existent row.** The event is lost.

3. **`failed` events**: `fail_billing_event` sets status to `failed`. Nothing ever retries `failed` events. The provider may resend the webhook, but `claim_billing_event` will return `duplicate` for the `(provider, event_id)` pair. **The event is permanently failed with no retry path.**

### Fix

**1. Handle `retry` status in `handleEvent`:**

```typescript
async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
  const claim = await this.store.claimBillingEvent(event.provider, event.eventId, event.eventType);
  if (claim.status === "duplicate") return { handled: true, action: "duplicate" };
  if (claim.status === "retry") {
    // Don't route — the claim failed, there's no event row to complete/fail
    return { handled: false, error: "claim_failed_retry" };
  }
  // ... existing try/catch
}
```

**2. Add a `reprocessStaleEvents` method to `BillingManager`:**

```python
# python/src/bursar/billing/manager.py
def reprocess_stale_events(self, older_than_seconds: int = 300) -> int:
    """Reprocess billing events stuck in 'processing' or 'failed' state."""
    stale = self._store.get_stale_billing_events(older_than_seconds)
    count = 0
    for event_row in stale:
        # Reset to 'processing' with a new claim attempt
        reclaimed = self._store.reclaim_billing_event(
            event_row["provider"], event_row["provider_event_id"]
        )
        if reclaimed:
            self._reprocess_event(event_row)
            count += 1
    return count
```

**3. Distinguish `processing` from `completed` in `claim_billing_event`:**

```sql
-- In claim_billing_event RPC: return "retry" (not "duplicate") for processing rows
IF v_existing.status = 'completed' THEN
    RETURN jsonb_build_object('status', 'duplicate');
ELSIF v_existing.status = 'processing' THEN
    -- Check if the processing row is stale (older than threshold)
    IF v_existing.created_at < now() - interval '5 minutes' THEN
        -- Reclaim it
        UPDATE billing_events SET status = 'processing', created_at = now()
        WHERE provider = p_provider AND provider_event_id = p_event_id;
        RETURN jsonb_build_object('status', 'claimed', 'event_id', v_existing.id);
    ELSE
        RETURN jsonb_build_object('status', 'retry');
    END IF;
ELSIF v_existing.status = 'failed' THEN
    -- Allow retry of failed events
    UPDATE billing_events SET status = 'processing'
    WHERE provider = p_provider AND provider_event_id = p_event_id;
    RETURN jsonb_build_object('status', 'claimed', 'event_id', v_existing.id);
ELSE
    -- New event
    INSERT ...
    RETURN jsonb_build_object('status', 'claimed', 'event_id', v_id);
END IF;
```

---

## [MEDIUM] #13 — `datetime.fromisoformat()` without try/except ✅ Fixed

### Location

`python/src/bursar/billing/manager.py:569`

### Code

```python
# python/src/bursar/billing/manager.py:566-571
def _provision_subscription(self, uid: str, offer: dict | None, event: BillingEvent) -> None:
    # ...
    period_start = None
    if event.subscription:
        ps = event.subscription.period_start
        period_start = datetime.fromisoformat(ps) if ps else None
        #                  ^^^^^^^^^^^^^^^^^^^^
        #                  Raises ValueError on malformed ISO strings
        #                  Provider timestamps may not be valid ISO 8601

    self._cm.set_user_plan(uid, plan_key, plan_assigned_at=period_start)
```

### Impact

If a payment provider sends a `period_start` that isn't valid ISO 8601 (e.g. Unix epoch integer, non-ISO format, empty string after whitespace strip), `datetime.fromisoformat()` raises `ValueError`. This exception propagates up through `_apply_subscription_event` → `handle_event`'s `except` block → `fail_billing_event`. The subscription event is marked as `failed` and never retried (see #6).

The JS equivalent uses `new Date(event.subscription.periodStart)` which returns `Invalid Date` instead of throwing — a different (but also problematic) failure mode:

```typescript
// javascript/src/billing/billing-manager.ts:649-651
const planAssignedAt = event.subscription?.periodStart
  ? new Date(event.subscription.periodStart)  // ← Invalid Date, not a throw
  : undefined;
```

### Fix

```python
# python/src/bursar/billing/manager.py:566-571
period_start = None
if event.subscription:
    ps = event.subscription.period_start
    if ps:
        try:
            period_start = datetime.fromisoformat(ps)
        except (ValueError, TypeError):
            logger.warning("invalid period_start timestamp %r for user %s, using now()", ps, uid)
            period_start = None  # Fall back to now() in set_user_plan
```

---

## [MEDIUM] #15 — Refund clawback uses default 1000 when payment metadata is missing ✅ Fixed

### Location

- `python/src/bursar/billing/manager.py:509-521`
- `javascript/src/billing/billing-manager.ts:589-605`

### Code

```python
# python/src/bursar/billing/manager.py:509-521
if refund.provider_payment_id and self._cm:
    payment = self._store.get_billing_payment(event.provider, refund.provider_payment_id)
    if payment and payment.get("purpose") == "credit_topup":
        pay_meta = payment.get("metadata") or {}
        credits_per_major = int(pay_meta.get("credits_per_major_unit", 1000))
        #                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        #                          Default 1000 if metadata is missing
        credits = (refund.amount_minor * credits_per_major) // 100
        if credits > 0:
            self._cm.deduct_credits(
                uid,
                Decimal(str(credits)),
                tx_type="refund_clawback",
                tier="purchased",
            )
```

The JS equivalent:

```typescript
// javascript/src/billing/billing-manager.ts:597-605
const payMeta = (payment?.metadata as Record<string, unknown>) ?? {};
const creditsPerMajor = Number(payMeta.creditsPerMajorUnit ?? 1000);
//                                                 ^^^^^^^^^^^^^^^^^
//                                                 Default 1000 if missing
const credits = Math.trunc((refund.amountMinor * creditsPerMajor) / 100);
```

### Impact

When a refund webhook arrives, bursar looks up the original payment to determine how many credits to claw back. The `credits_per_major_unit` value is stored in the payment's `metadata` when `handlePaymentSucceeded` processes the payment (line 516 in JS, line 432 in Python). But if:

1. The payment was processed before the metadata-storing code was added (legacy data).
2. The payment's `metadata` field is null in the DB.
3. The `billing_payments` row was created by a code path that doesn't set metadata (e.g. `handlePaymentFailed` at line 482-489 in Python, which calls `upsert_billing_payment` without metadata).

...then the clawback uses `1000` as the default. If the actual topup config used a different rate (e.g. `500`), the clawback deducts **double** the correct amount.

**Financial impact**: A user bought 5,000 credits at 500/major. They request a refund. The clawback calculates `(2000 * 1000) / 100 = 20,000` credits to deduct — but the user only has 5,000. The deduction will either fail (insufficient credits) or send the balance negative (overdraft mode).

### Fix

**Option A — Refuse clawback when metadata is missing:**

```python
if payment and payment.get("purpose") == "credit_topup":
    pay_meta = payment.get("metadata") or {}
    credits_per_major = pay_meta.get("credits_per_major_unit")
    if credits_per_major is None:
        logger.warning(
            "cannot claw back credits for refund %s: payment %s has no credits_per_major_unit metadata",
            refund.provider_refund_id, refund.provider_payment_id
        )
        return BillingEventResult(handled=True, action="refund_recorded_no_clawback")
    credits = (refund.amount_minor * int(credits_per_major)) // 100
```

**Option B — Look up the topup config dynamically:**

```python
if payment and payment.get("purpose") == "credit_topup":
    # Try metadata first, fall back to resolving the topup config
    pay_meta = payment.get("metadata") or {}
    credits_per_major = pay_meta.get("credits_per_major_unit")
    if credits_per_major is None:
        # Resolve from the payment's refs
        topup_config = self._store.resolve_credit_topup(
            event.provider,
            product_id=payment.get("product_id"),
            price_id=payment.get("price_id"),
        )
        if topup_config:
            credits_per_major = topup_config.get("credits_per_major_unit", 1000)
```

Option B is more robust but requires the payment row to store `product_id` / `price_id` refs, which it currently does not.

---

## [MEDIUM] #16 — JS `handleEvent` silently catches all exceptions ✅ Fixed

### Location

`javascript/src/billing/billing-manager.ts:62-68`

### Code

```typescript
// javascript/src/billing/billing-manager.ts:51-70
async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
  const claim = await this.store.claimBillingEvent(event.provider, event.eventId, event.eventType);
  if (claim.status === "duplicate") return { handled: true, action: "duplicate" };

  try {
    const result = await this.routeEvent(event);
    await this.store.completeBillingEvent(event.provider, event.eventId);
    return result;
  } catch (err) {
    await this.store.failBillingEvent(event.provider, event.eventId);
    return { handled: false, error: String(err) };
    //      ← error is stringified, original Error object is lost
    //      ← no rethrow, no logger.error, no emitter notification
  }
}
```

The Python equivalent at least logs the exception:

```python
# python/src/bursar/billing/manager.py:61-68
try:
    result = self._route_event(event)
    self._store.complete_billing_event(event.provider, event.event_id)
    return result
except Exception as exc:
    logger.exception("failed to handle billing event %s/%s", event.provider, event.event_id)
    self._store.fail_billing_event(event.provider, event.event_id)
    return BillingEventResult(handled=False, error=str(exc))
```

### Impact

In the JS SDK, billing event failures are completely silent:
1. No `logger.error` — the error is swallowed.
2. `String(err)` loses the stack trace and error type. `err.message` and `err.stack` are discarded.
3. No `CreditEventEmitter` notification — subscribers don't know a billing event failed.
4. The caller receives `{ handled: false, error: "Error: some message" }` — a string, not an Error object. They can't programmatically distinguish error types.
5. The webhook handler returns `{ received: true }` to the provider, so the provider thinks the webhook was processed successfully and won't resend it.

### Fix

```typescript
async handleEvent(event: BillingEvent): Promise<BillingEventResult> {
  const claim = await this.store.claimBillingEvent(event.provider, event.eventId, event.eventType);
  if (claim.status === "duplicate") return { handled: true, action: "duplicate" };
  if (claim.status === "retry") return { handled: false, error: "claim_failed" };

  try {
    const result = await this.routeEvent(event);
    await this.store.completeBillingEvent(event.provider, event.eventId);
    return result;
  } catch (err) {
    // Log the error with full context
    console.error(
      `[BillingManager] failed to handle billing event ${event.provider}/${event.eventId}`,
      err,
    );
    await this.store.failBillingEvent(event.provider, event.eventId);

    // Notify subscribers if an emitter is present
    this.emitter?.emit("billing_event_failed", { event, error: err });

    // Return the error message, but also preserve the Error for programmatic use
    return {
      handled: false,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}
```

Note: The `BillingManager` constructor doesn't currently accept a `logger` parameter. Add one (matching the Python SDK which uses `logging.getLogger(__name__)`).
