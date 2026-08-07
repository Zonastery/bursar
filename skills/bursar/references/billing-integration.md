# Billing integration: credit ledger ↔ payment provider

Bursar's billing capability turns payment-provider lifecycle events into
canonical credit mutations. Bursar owns the offer catalog and the normalized
event state machine; the provider (Stripe, Dodo, or a custom adapter) only
executes payments. The `commerce` section of `BursarConfig` declares providers,
offers, and auto-recharge guardrails.

## Mental model

```
provider webhook → adapter → normalized BillingEvent
  → ingest_billing_event(event)     # claims (provider, event_id, event_type)
  → lifecycle handler               # plan assignment, cycle grants, topups
  → canonical ledger entries        # idempotency-keyed, never replayed
```

Construct the facade with a billing store and commerce options; without
`billing_store`, `billing` and `commerce` are `None` and `ingest_billing_event`
raises `CommerceNotConfiguredError`. The two stores share the same database,
tenant, and migrations. `commerce_options` is a plain value in both SDKs: the
`CommerceOptions` model in Python, an object literal with the same fields
(`providers`, optional `defaultProvider`) in TypeScript.

```python
from bursar import Bursar, CommerceOptions, PostgresBillingStore, PostgresStore
from bursar.providers import StripeProvider

bursar = Bursar.create(
    credit_store=PostgresStore(database_url, tenant_id=tenant_id),
    billing_store=PostgresBillingStore(database_url, tenant_id=tenant_id),
    commerce_options=CommerceOptions(
        default_provider="stripe",
        providers={"stripe": lambda context: StripeProvider(context.event_sink,
                                                             webhook_secret=os.environ["STRIPE_WEBHOOK_SECRET"])},
    ),
)
```

```ts
import { Bursar, PostgresBillingStore, PostgresStore } from "@zonastery/bursar";
import { StripeProvider } from "@zonastery/bursar/providers";

const bursar = new Bursar({
  creditStore: new PostgresStore({ postgres: databaseUrl, tenantId }),
  billingStore: new PostgresBillingStore({ postgres: databaseUrl, tenantId }),
  commerceOptions: {
    defaultProvider: "stripe",
    providers: {
      stripe: (c) =>
        new StripeProvider(
          () => stripe,
          c.eventSink,
          process.env.STRIPE_WEBHOOK_SECRET,
        ),
    },
  },
});
```

## Offers and the offer catalog

`commerce.providers` names providers: `stripe`, `dodo`, or `custom` (with an
`adapter`). `commerce.offers` defines what customers buy; prices are integer
minor units with `currency` and optional `tax_behavior` (`inclusive` /
`exclusive` / `unspecified`), and provider references map offers to provider
objects (`stripe_price.price_id`, `dodo_product.product_id`,
`custom_object.external_id`).

**Subscription offers** bind a plan to a `billing_interval` (with optional
`trial`), an optional `cycle_grant` (`amount`, `bucket`, `renewal:
replace_previous | accumulate`, optional `expiry`), and `sort_order` /
`availability` for catalog display:

```yaml
offers:
  pro_monthly:
    type: subscription
    display_name: Pro Monthly
    price: { amount_minor: 2000, currency: USD }
    providers: { stripe: { type: stripe_price, price_id: price_pro_monthly } }
    plan: pro
    billing_interval: { unit: month, count: 1 }
    cycle_grant:
      { amount: "50000", bucket: purchased, renewal: replace_previous }
```

**Topup offers** sell credit packs: `credits_per_unit` credits per unit,
bounded `quantity: { minimum, maximum, default }`, a target `bucket`, optional
`expiry` (not `subscription_end`), and `lot_behavior` (`separate_lots` keeps
each purchase its own lot; `merge_and_refresh` merges). Resolve offers from
the active config by provider reference:

```python
offer = bursar.billing.resolve_offer("stripe", price_id="price_pro_monthly")
topup = bursar.billing.resolve_topup("stripe", price_id="price_credits_10k")
# offer.offer_key, offer.plan, offer.grant.credits/.bucket/.replace_prior
# topup.topup_key, topup.credits_per_unit, topup.amount_minor, topup.currency
```

`resolve_offer_by_lookup` / `resolve_topup_by_lookup` resolve by provider
lookup key; `invalidate_offer_cache()` drops the cache after a new publish.

## Subscriptions and provider events

`BillingEventType` covers the provider lifecycle (all values from
`billing/types`): `customer.created` / `customer.updated` / `customer.deleted`;
`checkout.completed` / `checkout.expired`;
`subscription.created` / `subscription.updated` / `subscription.activated` /
`subscription.renewed` / `subscription.plan_changed` / `subscription.cancellation_scheduled` /
`subscription.cancellation_unscheduled` / `subscription.canceled` / `subscription.expired` /
`subscription.paused` / `subscription.resumed` / `subscription.trial_will_end`;
`invoice.created` / `invoice.finalized` / `invoice.finalization_failed` /
`invoice.upcoming` / `invoice.paid` / `invoice.payment_failed` /
`invoice.payment_action_required` / `invoice.voided`;
`payment.succeeded` / `payment.failed`; `refund.created` / `refund.updated` /
`refund.failed`; `dispute.created` / `dispute.closed`;
`payment_method.attached` / `payment_method.updated` / `payment_method.detached`.
`BillingSubscriptionStatus` is `incomplete`, `incomplete_expired`, `trialing`,
`active`, `past_due`, `canceled`, `unpaid`, `paused`, `expired`. How events
become mutations (from the `BillingService` handler table):

| Event                                                                               | Mutation                                                                                                              |
| ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `subscription.created` / `activated` / `plan_changed` (positive state)              | Assign the offer's plan via the provisioning port (`set_user_plan`), anchoring the allowance window to `period_start` |
| `subscription.renewed`, or `invoice.paid` / `payment.succeeded` with a subscription | Post the cycle grant as an idempotency-keyed ledger entry; `replace_previous` expires last cycle's leftover first     |
| `subscription.canceled` / `expired` / `paused` / `customer.deleted`                 | Revoke the plan (unset it, or move to `terminal_plan_key`) — only when that subscription is the user's current one    |
| `payment.succeeded` with `purpose: "credit_topup"`                                  | Resolve the topup, compute credits (`credits_per_unit` × quantity), post a `purchase`-type grant                      |
| `refund.created` for that payment                                                   | Claw the grant back via a `refund_clawback` ledger entry                                                              |

A `past_due` subscription enters a grace period; when the grace end passes,
`expire_past_due_grace_periods` sweeps expired periods, revokes the plan, and
records `grace_expired_at`. Events that identify the user only through the
customer or subscription are resolved from persisted billing state first.

```python
from datetime import UTC, datetime
from bursar.billing.types import (BillingCustomerInfo, BillingEvent, BillingEventType,
                                  BillingSubscriptionInfo, ProviderRef)

bursar.ingest_billing_event(BillingEvent(
    provider="stripe", event_id="evt_1Paid",
    event_type=BillingEventType.subscription_created,
    occurred_at=datetime.now(UTC).isoformat(),
    user_id=user_id,
    customer=BillingCustomerInfo(provider_customer_id="cus_123", email="ada@prompta.ai"),
    subscription=BillingSubscriptionInfo(
        provider_subscription_id="sub_123", status="active",
        refs=ProviderRef(price_id="price_pro_monthly"),
        interval="month", interval_count=1,
    ),
))
```

```ts
import { BillingEvent, BillingEventType } from "@zonastery/bursar";

await bursar.ingestBillingEvent({
  provider: "stripe",
  eventId: "evt_1Paid",
  eventType: BillingEventType.subscription_created,
  occurredAt: new Date().toISOString(),
  userId,
  customer: { providerCustomerId: "cus_123", email: "ada@prompta.ai" },
  subscription: {
    providerSubscriptionId: "sub_123",
    status: "active",
    refs: { priceId: "price_pro_monthly" },
    interval: "month",
    intervalCount: 1,
  },
});
```

## Checkout

`create_checkout` has three phases: resolve the offer, enforce quantity bounds
and existing subscriptions, and record a **checkout intent** (the intent
exists before any money moves); the user pays on the provider's site; the
provider webhook (`checkout.completed`) completes the intent, which is what
grants credits or assigns the plan. Because the webhook is the only settlement
path, a payment that never touched your checkout endpoint still settles
through `ingest_billing_event`. Intent statuses: `open`, `completed`,
`failed`, `expired`.

```python
from bursar.commerce.types import CreateCheckoutInput

checkout = await bursar.commerce.create_checkout(CreateCheckoutInput(
    subject_id=user_id, account_id=user_id, offer_key="credits_10k",
    return_url="https://prompta.example/checkout/return/{intentId}",
    cancel_url="https://prompta.example/checkout/cancel/{intentId}",
    operation_key="op_checkout_topup_1", quantity=1,
    provider="dodo", type="credit_pack",
))
print(checkout.intent_id, checkout.url, bursar.commerce.get_checkout_status(checkout.intent_id, user_id).status)
```

`bursar.commerce.create_portal_session(account_id, return_url, purpose="billing" | "payment-method")` / `createPortalSession(input)` opens a provider portal session.

## Plan changes

`commerce.subscription_changes` configures behavior per direction —
`upgrade`, `downgrade`, `lateral`, `cadence_change` — each a
`SubscriptionChangePolicy` with `effective` (`immediate` | `renewal`),
`proration` (`prorated` | `none`), and `payment_failure` (`prevent_change`
default | `apply_change`). At runtime, `preview_plan_change` quotes the
provider and returns a `quote_fingerprint`; `confirm_plan_change` re-quotes
and rejects the change if the quote moved (`QuoteChangedError`). Scheduled
changes persist as billing subscription changes and can be cancelled first.
(Commerce-parity sample: upgrades/laterals `immediate` + `prorated`,
downgrades/cadence changes `renewal` + `none`.)

```python
preview = await bursar.commerce.preview_plan_change(user_id, offer_key="pro_monthly")
confirmed = await bursar.commerce.confirm_plan_change(
    user_id, "downgrade-1", offer_key="pro_monthly", quote_fingerprint=preview.quote_fingerprint,
)
```

```ts
const preview = await bursar.commerce.previewPlanChange({
  accountId: userId,
  offerKey: "pro_monthly",
});
const confirmed = await bursar.commerce.confirmPlanChange({
  accountId: userId,
  operationKey: "downgrade-1",
  offerKey: "pro_monthly",
  quoteFingerprint: preview.quoteFingerprint,
});
```

## Auto-recharge guardrails

`commerce.auto_recharge` turns a low balance into a topup purchase without
user interaction. All keys are required: `eligible_topups` (topup offers the
policy may buy), `balance_below` (`minimum`/`maximum`/`default` credits
trigger), `rearm_above` (balance that arms it again — **must exceed**
`balance_below.maximum`), `quantity`, and `limits` (`max_purchases` per
`window`, `max_charge_minor` in minor units, `cooldown` duration,
`max_consecutive_failures` default 3, `failure_action` const `pause`).
Validation also requires one currency across eligible topups and quantity
bounds that fit each offer.

The facade hooks `auto_recharge.process_if_needed` after every deduction; a
payment attempt is claimed per user so concurrent deductions cannot start
duplicate purchases. Users opt in via `bursar.commerce.auto_recharge.enable`
(with `return_url`); `get_status` reports recharges in the window; outcomes
are `not_configured`, `disabled`, `above_threshold`, `already_processing`,
`limit_reached`, `submitted`, `action_required`, `failed`. Per-user profiles
persist threshold, topup, quantity, window counts, and payment method, so
recharges are idempotent and bounded across restarts; the resulting
`payment.succeeded` webhook grants the topup and re-arms the profile.

## Event handling: idempotency, retries, webhook safety

- **Claim first:** `ingest_billing_event` claims each event on
  `(provider, event_id, event_type)` before routing, so redelivered webhooks
  are ignored and subscription grants never double-post. `BillingEventClaim`
  statuses are `claimed`, `duplicate`, `busy`, `retry`.
- **Ledger-level idempotency:** credit grants (cycle grants, topups,
  `grant_subscription_cycle`) are idempotency-keyed canonical ledger entries;
  replaying `deduct` with the same key is a no-op.
- **Signed payloads:** adapters verify provider webhook signatures (e.g.
  Stripe webhook secret, Dodo signature verification) before mapping to
  events; `commerce.handle_webhook(raw_body, headers, provider)` is the
  convenience entry for provider payloads.
- **Bounded retries:** `retry_bursar_operation` wraps catalog/config reads so
  transient database failures retry; failed claims surface as `retry`/`busy`
  rather than double-applying.
- **Provider-neutral events:** events arrive as `BillingEvent` with
  `event_id`, `event_type`, `occurred_at` (must include a timezone, normalized
  to UTC), `user_id`, and optional `customer`, `subscription`, `invoice`,
  `payment`, `refund`, `dispute`, `metadata`, `raw` payloads.
