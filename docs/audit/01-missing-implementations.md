# Missing Implementations

Stubs, no-ops, aspirational event types, and interface methods with incomplete concrete implementations.

## Fix Status

| Finding | Severity | Status |
|---|---|---|
| #4 — Python zero provider implementations | HIGH | ✅ Fixed |
| #7 — Dodo `listPaymentMethods` returns `[]` | HIGH | ✅ Fixed |
| #12 — 33 event types, ~10 emitted | MEDIUM | ✅ Fixed |
| #14 — Customer no-ops (updated/deleted) | MEDIUM | ✅ Fixed |
| #24 — Stripe setup session identical to update | LOW | ✅ Fixed |
| #29 — Dodo portal no `return_url` | LOW | ✅ Fixed |
| #30 — Dodo customer no `metadata` | LOW | ✅ Fixed |
| #31 — Dodo `getInvoiceUrl` fallback | LOW | ✅ Fixed (best-effort, Dodo SDK limitation) |
| #32 — `trial_will_end` no-op | LOW | ✅ Fixed |
| #34 — Unhandled event types silently ignored | LOW | ✅ Fixed |

**10/10 fixed.**

---

## [HIGH] #4 — Python ships zero payment provider implementations ✅ Fixed

### Location

- `python/src/bursar/providers/types.py:70-101` — `PaymentProvider` ABC (interface only)
- `python/src/bursar/providers/__init__.py` — empty file
- JS counterparts: `javascript/src/providers/stripe/`, `javascript/src/providers/dodo/`, `javascript/src/providers/mock/`

### Code

Python defines the interface but provides no concrete classes:

```python
# python/src/bursar/providers/types.py:70-101
class PaymentProvider(ABC):
    provider: str

    @abstractmethod
    async def create_checkout_session(self, params: CheckoutParams) -> dict: ...

    @abstractmethod
    async def create_customer_portal_session(self, params: PortalParams) -> dict: ...

    @abstractmethod
    async def handle_webhook(self, req: WebhookRequest) -> dict: ...

    @abstractmethod
    async def cancel_subscription(self, subscription_id: str) -> None: ...

    # ... 10 abstract methods total, zero implementations
```

```python
# python/src/bursar/providers/__init__.py  — 0 bytes, completely empty
```

JS ships three concrete providers:

```
javascript/src/providers/
├── stripe/     — provider.ts (151 lines) + event-mapper.ts (277 lines)
├── dodo/       — provider.ts (137 lines) + event-mapper.ts (181 lines)
├── mock/       — provider.ts (91 lines)
├── types.ts    — interface (84 lines)
└── index.ts    — exports all three
```

### Impact

A Python SaaS developer adopting bursar gets the full `BillingManager` + `BillingStore` stack (memory, postgres, supabase) and can handle webhooks — but must write their own Stripe/Dodo adapter from scratch. The JS SDK is the only one that ships ready-to-use provider integrations. This asymmetry contradicts the "complete credit management system with in-built subscription management and payment integrations" positioning for Python users.

### Fix

Port the JS provider implementations to Python. The `stripe` and `dodopayments` Python packages exist on PyPI. The event-mapper logic is pure translation (no JS-specific constructs). Create:

```
python/src/bursar/providers/
├── __init__.py          — re-export StripeProvider, DodoProvider, MockPaymentProvider
├── types.py             — (existing) PaymentProvider ABC
├── stripe/
│   ├── __init__.py
│   ├── provider.py      — StripeProvider using `stripe` Python SDK
│   └── event_mapper.py  — handle_stripe_webhook()
├── dodo/
│   ├── __init__.py
│   ├── provider.py      — DodoProvider using `dodopayments` Python SDK
│   └── event_mapper.py  — handle_dodo_billing_event()
└── mock/
    ├── __init__.py
    └── provider.py      — MockPaymentProvider
```

The `stripe` and `dodopayments` packages should be **optional dependencies** (`pip install bursar[stripe]`, `pip install bursar[dodo]`) to avoid forcing them on all users.

---

## [HIGH] #7 — Dodo `listPaymentMethods` returns empty array ✅ Fixed

### Location

`javascript/src/providers/dodo/provider.ts:119-121`

### Code

```typescript
// javascript/src/providers/dodo/provider.ts:119-121
async listPaymentMethods(_customerId: string): Promise<PaymentMethodInfo[]> {
  return [];
}
```

### Impact

The billing dashboard API (`web/app/api/billing/route.ts`) calls `provider.listPaymentMethods(customerId)` to display saved cards. With Dodo as the active provider, the user sees an empty list even if they have saved payment methods. The `PaymentProvider` interface promises this data; the Dodo implementation silently lies.

### Fix

The Dodo SDK has a `customers.wallets` sub-resource (confirmed in the SDK types). Use it:

```typescript
async listPaymentMethods(customerId: string): Promise<PaymentMethodInfo[]> {
  const client = this.getClient();
  const response = await client.customers.wallets.list(customerId);
  return (response.items ?? []).map((w) => ({
    id: w.payment_method_id ?? "",
    last4: w.last4 ?? "",
    brand: w.brand ?? "unknown",
    expiryMonth: w.exp_month ?? 0,
    expiryYear: w.exp_year ?? 0,
  }));
}
```

If the Dodo wallets API doesn't expose enough detail, return a `PaymentMethodInfo[]` with a single entry indicating a saved method exists but details are unavailable — better than an empty array that implies no methods exist.

---

## [MEDIUM] #12 — 33 `BillingEventType`s defined, only ~10 emitted ✅ Fixed

### Location

- `javascript/src/billing/billing-types.ts:10-45` — enum definition
- `python/src/bursar/billing/models.py:14-49` — enum definition
- `javascript/src/providers/stripe/event-mapper.ts` — emits 7 event types
- `javascript/src/providers/dodo/event-mapper.ts` — emits 10 event types

### Code

33 event types are defined:

```typescript
// javascript/src/billing/billing-types.ts:10-45
export type BillingEventType =
  | "customer.created" | "customer.updated" | "customer.deleted"
  | "checkout.completed" | "checkout.expired"
  | "subscription.created" | "subscription.updated" | "subscription.activated"
  | "subscription.renewed" | "subscription.plan_changed"
  | "subscription.cancellation_scheduled" | "subscription.cancellation_unscheduled"
  | "subscription.canceled" | "subscription.expired"
  | "subscription.paused" | "subscription.resumed" | "subscription.trial_will_end"
  | "invoice.created" | "invoice.finalized" | "invoice.finalization_failed"
  | "invoice.upcoming" | "invoice.paid" | "invoice.payment_failed"
  | "invoice.payment_action_required" | "invoice.voided"
  | "payment.succeeded" | "payment.failed"
  | "refund.created" | "refund.updated" | "refund.failed"
  | "dispute.created" | "dispute.closed"
  | "payment_method.attached" | "payment_method.updated" | "payment_method.detached";
```

The Stripe event-mapper only emits: `checkout.completed`, `subscription.created`, `subscription.activated`, `subscription.canceled`, `subscription.cancellation_scheduled`, `subscription.updated`, `invoice.paid`, `payment.succeeded` (8 types).

The Dodo event-mapper only emits: `subscription.created`, `subscription.activated`, `subscription.canceled`, `subscription.expired`, `subscription.updated`, `subscription.cancellation_scheduled`, `subscription.plan_changed`, `subscription.paused`, `payment.succeeded` (9 types).

The `BillingManager` handler map covers 21 types. **23 of the 33 defined event types are never emitted by any provider.** Unknown types are silently ignored:

```python
# python/src/bursar/billing/manager.py:95-99
handler = handlers.get(event.event_type)
if handler is None:
    logger.debug("unhandled billing event type %s", event.event_type)
    return BillingEventResult(handled=True, action="ignored")
```

### Impact

The over-provisioned enum creates a false sense of coverage. A developer reading the types assumes all 33 events are handled. The reality is that `checkout.expired`, `invoice.created`, `invoice.finalized`, `invoice.finalization_failed`, `invoice.upcoming`, `invoice.payment_failed`, `invoice.payment_action_required`, `invoice.voided`, `refund.updated`, `refund.failed`, `dispute.closed`, `payment_method.attached`, `payment_method.updated`, `payment_method.detached`, `subscription.renewed` (as a distinct type — it's actually handled as `subscription.activated`), `subscription.cancellation_unscheduled`, `subscription.trial_will_end` (handler is a no-op), `subscription.resumed`, `customer.updated` (no-op), `customer.deleted` (no-op) are all aspirational.

### Fix

Two options:

**Option A — Trim to reality**: Remove the 23 unused event types from the enum. Add them back when a provider actually emits them.

**Option B — Document aspiration**: Keep the full enum but add a `@emitted` / `@aspirational` JSDoc/docstring tag to each type. Create a test that asserts every handler-mapped event type is emitted by at least one provider's event-mapper.

```python
# Example: aspirational tag in models.py
class BillingEventType(StrEnum):
    checkout_completed = "checkout.completed"           # @emitted by stripe, dodo
    checkout_expired = "checkout.expired"               # @aspirational
    subscription_created = "subscription.created"       # @emitted by stripe, dodo
    # ...
```

---

## [MEDIUM] #14 — `_handle_customer_updated` / `_handle_customer_deleted` are no-ops ✅ Fixed

### Location

`python/src/bursar/billing/manager.py:238-242`

### Code

```python
# python/src/bursar/billing/manager.py:238-242
def _handle_customer_updated(self, event: BillingEvent) -> BillingEventResult:
    return BillingEventResult(handled=True, action="customer_updated")

def _handle_customer_deleted(self, event: BillingEvent) -> BillingEventResult:
    return BillingEventResult(handled=True, action="customer_deleted")
```

The JS equivalents are similarly empty — they return `{ handled: true, action: "customer_updated" }` without persisting anything.

### Impact

When a payment provider sends a `customer.updated` event (e.g. email change in Stripe dashboard) or `customer.deleted` event, bursar marks the event as `handled: true` but does nothing. The `billing_customers` table is never updated. This means:
- Customer email changes at the provider are not reflected in bursar.
- Customer deletion at the provider does not trigger subscription cancellation or credit revocation in bursar.

A `customer.deleted` event with no handler is particularly dangerous — the user's subscriptions and credits remain active even though the provider customer no longer exists.

### Fix

```python
def _handle_customer_updated(self, event: BillingEvent) -> BillingEventResult:
    if event.customer and event.customer.provider_customer_id:
        uid = self._resolve_user_id(event)
        if uid:
            self._store.upsert_billing_customer(
                event.provider,
                event.customer.provider_customer_id,
                uid,
                event.customer.email,
            )
    return BillingEventResult(handled=True, action="customer_updated")

def _handle_customer_deleted(self, event: BillingEvent) -> BillingEventResult:
    if event.customer and event.customer.provider_customer_id:
        uid = self._resolve_user_id(event)
        if uid and self._cm:
            self._revoke_subscription(uid)
    return BillingEventResult(handled=True, action="customer_deleted")
```

Note: `_handle_customer_deleted` should also consider whether to cancel active subscriptions and revoke credits, or whether to just log a warning and leave the decision to the application layer. At minimum, it should **not** silently return `handled: true`.

---

## [LOW] #24 — Stripe `createPaymentMethodSetupSession` identical to `createUpdatePaymentMethodSession` ✅ Fixed

### Location

`javascript/src/providers/stripe/provider.ts:67-91`

### Code

```typescript
// javascript/src/providers/stripe/provider.ts:67-78
async createUpdatePaymentMethodSession(
  params: UpdatePaymentMethodParams,
): Promise<{ url: string }> {
  const stripe = this.getStripe();
  const session = await stripe.billingPortal.sessions.create({
    customer: params.customerId,
    return_url: params.returnUrl,
    flow_data: { type: "payment_method_update" },
  });
  if (!session.url) throw new Error("Stripe portal session returned no URL");
  return { url: session.url };
}

// javascript/src/providers/stripe/provider.ts:80-91  — IDENTICAL
async createPaymentMethodSetupSession(
  params: PaymentMethodSetupParams,
): Promise<{ url: string }> {
  const stripe = this.getStripe();
  const session = await stripe.billingPortal.sessions.create({
    customer: params.customerId,
    return_url: params.returnUrl,
    flow_data: { type: "payment_method_update" },  // ← same flow type
  });
  if (!session.url) throw new Error("Stripe portal session returned no URL");
  return { url: session.url };
}
```

Both methods use `flow_data: { type: "payment_method_update" }`. The "setup" method should use Stripe's Checkout Session with `mode: "setup"` to allow adding a new payment method without updating an existing one.

### Impact

Calling `createPaymentMethodSetupSession` updates the existing payment method instead of setting up a new one. The two interface methods are semantically different but behave identically.

### Fix

```typescript
async createPaymentMethodSetupSession(
  params: PaymentMethodSetupParams,
): Promise<{ url: string }> {
  const stripe = this.getStripe();
  const session = await stripe.checkout.sessions.create({
    customer: params.customerId,
    mode: "setup",
    success_url: params.returnUrl,
    cancel_url: params.cancelUrl ?? params.returnUrl,
    payment_method_types: ["card"],
  });
  if (!session.url) throw new Error("Stripe setup session returned no URL");
  return { url: session.url };
}
```

---

## [LOW] #29 — Dodo `createCustomerPortalSession` doesn't pass `return_url` ✅ Fixed

### Location

`javascript/src/providers/dodo/provider.ts:40-44`

### Code

```typescript
// javascript/src/providers/dodo/provider.ts:40-44
async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
  const client = this.getClient();
  const session = await client.customers.customerPortal.create(params.customerId);
  return { url: session.link };
}
```

The `PortalParams` includes `returnUrl` but it's never passed to the Dodo SDK. The user won't be redirected back to the app after leaving the portal.

### Impact

After a user finishes in the Dodo customer portal, they are not redirected back to the application. This is a poor UX — they're left on the Dodo page with no way back.

### Fix

Check the Dodo SDK's `CustomerPortalCreateParams` for a `return_url` field. If the SDK supports it:

```typescript
async createCustomerPortalSession(params: PortalParams): Promise<{ url: string }> {
  const client = this.getClient();
  const session = await client.customers.customerPortal.create(params.customerId, {
    return_url: params.returnUrl,
  });
  return { url: session.link };
}
```

If the Dodo SDK doesn't support `return_url` in the portal API (the type may only have `send_email`), document this as a Dodo SDK limitation and fall back to the app's own return URL handling.

---

## [LOW] #30 — Dodo `createCustomer` doesn't pass `metadata` ✅ Fixed

### Location

`javascript/src/providers/dodo/provider.ts:123-130`

### Code

```typescript
// javascript/src/providers/dodo/provider.ts:123-130
async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
  const client = this.getClient();
  const customer = await client.customers.create({
    email: params.email,
    name: params.name,
    // metadata: params.metadata  ← not passed
  });
  return { customerId: customer.customer_id };
}
```

`CreateCustomerParams` includes `metadata: Record<string, string>` but it's not forwarded to the Dodo SDK. The Stripe provider correctly passes it (`stripe/provider.ts:141`).

### Impact

Customer metadata (e.g. `userId`) is not stored on the Dodo customer record. This makes it harder to cross-reference Dodo customers with app users via the provider's dashboard.

### Fix

```typescript
async createCustomer(params: CreateCustomerParams): Promise<{ customerId: string }> {
  const client = this.getClient();
  const customer = await client.customers.create({
    email: params.email,
    name: params.name,
    ...(params.metadata ? { metadata: params.metadata } : {}),
  });
  return { customerId: customer.customer_id };
}
```

Note: Verify that `CustomerCreateParams` in the Dodo SDK supports `metadata`. If not, this is a Dodo SDK limitation that should be documented.

---

## [LOW] #31 — Dodo `getInvoiceUrl` reads `payment.payment_link` — field may not exist ✅ Fixed (best-effort)

### Location

`javascript/src/providers/dodo/provider.ts:132-136`

### Code

```typescript
// javascript/src/providers/dodo/provider.ts:132-136
async getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null> {
  const client = this.getClient();
  const payment = await client.payments.retrieve(providerPaymentId);
  return payment.payment_link ? { url: payment.payment_link } : null;
}
```

The Dodo `Payment` type has a `payment_link` field (a URL to the hosted payment page), but this is not the same as an invoice URL. The Stripe provider correctly reads `invoice.hosted_invoice_url` from the Stripe `Invoice` type.

### Impact

Users clicking "View Invoice" may be redirected to the Dodo checkout page instead of a proper invoice document.

### Fix

Check whether Dodo has an `invoices` resource (`client.invoices.retrieve(id)`). If so:

```typescript
async getInvoiceUrl(providerPaymentId: string): Promise<{ url: string } | null> {
  const client = this.getClient();
  // If Dodo has an invoices API:
  const invoice = await client.invoices.retrieve(providerPaymentId);
  return invoice.hosted_invoice_url ? { url: invoice.hosted_invoice_url } : null;
}
```

If Dodo doesn't have a separate invoices API, document that `payment_link` is the closest available equivalent.

---

## [LOW] #32 — `_handle_trial_will_end` is a no-op notification ✅ Fixed

### Location

`python/src/bursar/billing/manager.py:395-396`, `javascript/src/billing/billing-manager.ts:473`

### Code

```python
# python/src/bursar/billing/manager.py:395-396
def _handle_trial_will_end(self, event: BillingEvent) -> BillingEventResult:
    return BillingEventResult(handled=True, action="trial_will_end_notified")
```

```typescript
// javascript/src/billing/billing-manager.ts:473
private async handleTrialWillEnd(_event: BillingEvent): Promise<BillingEventResult> {
  return { handled: true, action: "trial_will_end_notified" };
}
```

### Impact

When a subscription trial is about to end, bursar marks the event as handled but does nothing. No email is sent, no webhook is fired to the application, no state is updated. The `CreditEventEmitter` is not notified. A SaaS app using bursar would expect to be able to hook into this event to send a "your trial is ending" email.

### Fix

At minimum, emit a `CreditEvent` via the `CreditEventEmitter` so the application can subscribe:

```python
def _handle_trial_will_end(self, event: BillingEvent) -> BillingEventResult:
    if self._emitter:
        uid = self._resolve_user_id(event)
        if uid:
            self._emitter.emit("trial_will_end", {"user_id": uid, "event": event})
    return BillingEventResult(handled=True, action="trial_will_end_notified")
```

Alternatively, add a callback hook to `BillingManager.__init__` (similar to `resolve_user`) for trial-end notifications.

---

## [LOW] #34 — `BillingEventType` includes events with no handler — silently ignored ✅ Fixed

### Location

- `python/src/bursar/billing/manager.py:71-94` — handler map (21 entries)
- `javascript/src/billing/billing-manager.ts:73-96` — handler map (22 entries)
- `javascript/src/billing/billing-types.ts:10-45` — 33 event types defined

### Code

Events defined in the enum but NOT in the handler map:

```
checkout.expired
invoice.created
invoice.finalized
invoice.finalization_failed
invoice.upcoming
invoice.payment_action_required
invoice.voided
refund.updated
refund.failed
payment_method.attached
payment_method.updated
payment_method.detached
```

When any of these arrives, the handler map lookup returns `None` / `undefined` and the event is silently ignored:

```python
# python/src/bursar/billing/manager.py:95-99
handler = handlers.get(event.event_type)
if handler is None:
    logger.debug("unhandled billing event type %s", event.event_type)
    return BillingEventResult(handled=True, action="ignored")
```

The event is marked as `handled: true` with `action: "ignored"` — which means the `billing_events` row is marked as `completed`. If a handler is later added, the event will NOT be reprocessed (it's already `completed`).

### Impact

1. Events that should trigger actions (e.g. `refund.failed` should alert operations, `invoice.payment_action_required` should notify the user of 3DS requirements) are silently dropped.
2. Marking them as `completed` means they can never be reprocessed, even after handlers are added.
3. The `debug` log level means these are invisible in production logs unless debug logging is enabled.

### Fix

**Short-term**: Change unhandled events to `completed` only if they're explicitly in an "ignore list". For everything else, mark as `failed` so they can be retried:

```python
IGNORED_EVENT_TYPES = frozenset({
    "checkout.expired",  # No action needed
    "invoice.upcoming",  # Notification only, no state change
})

handler = handlers.get(event.event_type)
if handler is None:
    if event.event_type in IGNORED_EVENT_TYPES:
        return BillingEventResult(handled=True, action="ignored")
    logger.warning("unhandled billing event type %s (marking as failed)", event.event_type)
    return BillingEventResult(handled=False, error="unhandled_event_type")
```

**Long-term**: Add handlers for all 33 event types, or trim the enum to only types with handlers.
