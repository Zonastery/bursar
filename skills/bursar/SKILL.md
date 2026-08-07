---
name: bursar
description: >-
  Integrate Bursar, the credit billing engine for AI SaaS applications, into a
  Python or TypeScript backend. Use when adding usage metering, prepaid credits,
  a credit ledger, plans and allowances, subscriptions, quotas, spend caps,
  leases for long-running AI work, idempotent billing, pricing configs and rate
  cards, auto-recharge, or billing events. Triggers: credits, billing,
  entitlements, ledger, leases, topups, usage charges, meter AI tokens, Bursar
  CLI, @zonastery/bursar, pip install bursar. Covers the Bursar facade, the
  PostgresStore, the canonical BursarConfig document, the bursar migrate and
  tenant/config CLI commands, and the Python and TypeScript SDKs.
license: AGPL-3.0
compatibility: "Requires PostgreSQL 16+ (pg_partman 5.x); Python 3.12+ or Node.js 22+; network access to install packages"
metadata:
  author: zonastery
  version: "1.0"
---

# bursar — agentic integration skill

Bursar is the credit billing engine for AI SaaS. One `Bursar` facade in front
of one PostgreSQL schema gives you metered usage pricing, prepaid credit
balances, plans with free allowances, quotas and spend caps, atomic leases for
long-running AI work, and an optional payment-provider billing layer
(subscriptions, topups, auto-recharge) — all on an append-only, idempotent,
exact-decimal credit ledger driven by a single strict versioned
`BursarConfig`. Two parallel SDKs expose the same API: Python `pip install
bursar[postgres]` (`from bursar import Bursar`) and TypeScript `npm install
@zonastery/bursar` (`import { Bursar } from "@zonastery/bursar"`).

## When to use

Reach for this skill when the task involves any of:

- Metering AI usage and charging for it — tokens, tool calls, jobs, compute.
- Prepaid credits: grants, topups, purchases, refunds, expiry, revocation.
- A credit **ledger**: append-only money history with per-entry balances.
- **Plans and allowances**, **subscriptions**, **quotas and spend caps**.
- **Leases**: reserving capacity for long-running work, settling actual cost.
- **Idempotent billing**: replay-safe webhook handling, no double charges.
- **Pricing configs and rate cards**: operations, measures, price rules,
  expression formulas.
- **Auto-recharge** and **billing events**: normalized provider events via
  `ingest_billing_event`.

Keywords: credits, billing, entitlements, ledger, leases, `bursar migrate`,
`bursar tenant create`, `bursar config set`, `@zonastery/bursar`,
`pip install bursar`, `PostgresStore`, `Bursar.create`.

## Mental model

The facade is the application boundary: `bursar.credits` (balances, ledger,
lots, leases, allowances, quotas, analytics), `bursar.catalog` (publish and
activate the versioned config), `bursar.accounts` (default plan + signup
grants), and, when a `billing_store` is supplied, `bursar.billing` and
`bursar.commerce` (checkout, offers, auto-recharge).
`PostgresStore`/`PostgresBillingStore` are the only stores; both are
tenant-bound and share one migrated schema. Construct one facade; never wire
credit and billing services together independently.

## Hard rules

1. **Exact decimal money, never floats.** All amounts are `decimal.Decimal`
   (Python) / `decimal.js` `Decimal` (TypeScript); storage is `numeric(20,6)`;
   results quantize to 6dp with `ROUND_HALF_UP`. In config documents every
   exact decimal value is a **string**.
2. **Append-only ledger.** Every mutation appends a `credit_ledger_entries`
   row with a `balance_after` equal to the resulting account balance; entries
   are never updated or deleted. `credit_accounts.balance` is written only by
   the ledger-posting path under a row lock.
3. **Stable per-account idempotency keys on every replayable monetary write.**
   `(account_id, idempotency_key)` is unique, so a retry replays the original
   entry (`idempotent=True`) instead of double-posting. Reusing a key with a
   _different_ request is a conflict.
4. **Never bypass the SDK.** No raw SQL writes to Bursar tables, no host
   migration that creates/alters/seeds Bursar tables, no direct inserts into
   `bursar.tenants`. `bursar migrate` is the only installation path; tenant
   lifecycle goes through the CLI.
5. **Always be tenant-bound.** Every store is constructed with a `tenant_id`;
   mandatory `tenant_id` columns, row-level security, and composite FKs
   isolate tenants. Never set a session-scoped tenant value on a pooled
   connection.
6. **Leases settle at most once.** `reserve` is the only authoritative
   admission gate; `settle` bills the full actual cost; `release` returns an
   unused hold; crashed workers' leases expire via TTL + reaper.

## Standard workflow

1. **Install the schema.** `pip install bursar[postgres]` (Python) or
   `npm install @zonastery/bursar` (Node 22+). With `DATABASE_URL` set, apply
   the ordered SQL baseline (idempotent; re-runs are no-ops):

   ```bash
   export DATABASE_URL=postgresql://...
   bursar migrate
   ```

2. **Provision a tenant.** The printed UUID is your tenant id:

   ```bash
   bursar tenant create acme
   export BURSAR_TENANT_ID=018f7f5f-7b4a-7000-8000-000000000001
   ```

   `bursar tenant bootstrap acme ./pricing.yaml --display-name "Acme"` does
   tenant + initial config in one idempotent command; `bursar tenant status
<id> suspended|active` flips lifecycle state.

3. **Write and publish the pricing config.** One strict, versioned
   document: `pricing` (operations, rate cards), `credits` (buckets,
   policies, grants), `entitlements` (features), `admission` (concurrency),
   `plans` (allowances, quotas, features, rate_card), `commerce` (providers,
   offers, auto_recharge), `catalog` (default_plan). Unknown fields are
   rejected; exact decimals are strings:

   ```yaml
   version: 1
   catalog:
     default_plan: free
   pricing:
     operations:
       completion:
         measures:
           input_tokens: { unit: token }
     rate_cards:
       standard:
         operations:
           completion:
             rules:
               - when:
                   model: { op: in, values: [gpt-4o, gpt-4o-mini] }
                 charge:
                   type: per_unit
                   measure: input_tokens
                   rate: "0.0025"
                   unit_size: "1000000"
             unmatched:
               action: reject
   credits:
     buckets:
       purchased: { priority: 10 }
     default_bucket: purchased
   plans:
     free:
       rate_card: standard
       credit_allowance:
         amount: "10000"
         priority: 5
         window: { type: calendar, unit: month, count: 1 }
   ```

   ```bash
   bursar config validate pricing.yaml   # --json for CI
   bursar config set pricing.yaml        # publish + activate (no-op if identical)
   bursar config get                     # active version as JSON
   bursar config activate 3              # roll back to an immutable version
   ```

4. **Construct the facade** — the store binds your tenant; both SDKs take the
   store from the package top level:

   ```python
   from bursar import Bursar, PostgresStore

   store = PostgresStore(database_url, tenant_id=tenant_id)
   bursar = Bursar.create(credit_store=store)
   ```

   ```ts
   import { Bursar, PostgresStore } from "@zonastery/bursar";

   const store = new PostgresStore({
     postgres: process.env.DATABASE_URL!,
     tenantId,
   });
   const bursar = new Bursar({ creditStore: store });
   ```

   Enable billing/commerce by also passing `billing_store` and
   `commerce_options` (`references/billing-integration.md`).

5. **Integrate metering and charges.** Register accounts on signup, grant
   credits, and charge usage with idempotency keys:

   ```python
   bursar.accounts.on_account_created(ada, event_key="signup")
   bursar.credits.add_credits(
       ada,
       1000,
       entry_type="purchase",
       idempotency_key="purchase:bootstrap",
   )

   from decimal import Decimal
   from bursar.metrics import UsageMetrics

   result = bursar.credits.deduct(
       ada,
       UsageMetrics(
           operation="completion",
           measures={"input_tokens": Decimal(800)},
           dimensions={"model": "gpt-4o"},
       ),
       idempotency_key="turn_12345",
   )
   print(result.amount, result.allowance_consumed, result.balance_after)
   ```

   ```ts
   await bursar.accounts.onAccountCreated({
     accountId: ada,
     eventKey: "signup",
   });
   await bursar.credits.addCredits(ada, 1000, {
     type: "purchase",
     idempotencyKey: "purchase:bootstrap",
   });

   const result = await bursar.credits.deduct(
     ada,
     {
       operation: "completion",
       measures: { input_tokens: 800 },
       dimensions: { model: "gpt-4o" },
     },
     { idempotencyKey: "turn_12345" },
   );
   console.log(result.amount, result.allowanceConsumed, result.balanceAfter);
   ```

   Every call above is replay-safe: re-running with the same key is a no-op.

## Common integration tasks

### Grant credits

```python
grant = bursar.credits.add_credits(
    user_id, 50000, entry_type="purchase", idempotency_key="checkout:cs_123456",
)
print(grant.entry_id, grant.new_balance, grant.bucket, grant.idempotent)
```

```ts
const grant = await bursar.credits.addCredits(userId, new Decimal(50000), {
  type: "purchase",
  idempotencyKey: "checkout:cs_123456",
});
console.log(grant.entryId, grant.newBalance, grant.bucket, grant.idempotent);
```

Pass `expires_at`/`expiresAt` for an expiring lot. Administrative clawbacks
use `deduct_credits(user_id, amount, entry_type="adjustment")` /
`deductCredits(userId, amount, { entryType: "adjustment" })`.

### Charge for usage

`deduct` prices the `UsageMetrics` through the plan's rate card and charges
atomically — allowance first, then buckets in priority order (lower number
first) — raising `InsufficientCreditsError` (HTTP 402) when the floor would
be crossed.

```python
from decimal import Decimal

charge = bursar.credits.deduct(
    user_id,
    UsageMetrics(
        operation="completion",
        measures={
            "input_tokens": Decimal("4000"),
            "output_tokens": Decimal("1200"),
            "cache_read_tokens": Decimal("6000"),
        },
        dimensions={"model": "gpt-4o-mini"},
    ),
    idempotency_key="chat:turn:42",
)
print(charge.amount, charge.allowance_consumed, charge.balance_after)
```

```ts
const charge = await bursar.credits.deduct(
  userId,
  {
    operation: "completion",
    measures: {
      input_tokens: 4000,
      output_tokens: 1200,
      cache_read_tokens: 6000,
    },
    dimensions: { model: "gpt-4o-mini" },
  },
  { idempotencyKey: "chat:turn:42" },
);
console.log(charge.amount, charge.allowanceConsumed, charge.balanceAfter);
```

`DeductionResult` fields: `amount` (net debit after allowance),
`allowance_consumed`, `balance_after`, `entry_id`, `idempotent`. Pass
`feature="voice_mode"` to enforce an entitlement at charge time.

### Reserve / settle a lease (long-running work)

`reserve` is the only authoritative admission gate (worst-case hold, policy
snapshot, quota + allowance checks in one transaction); `settle` bills the
full actual cost; `release` returns an unused hold.

```python
from bursar.credits.service_types import ReserveOptions, SettleOptions

lease = bursar.credits.reserve(
    user_id,
    estimate_metrics,
    ReserveOptions(ttl=120, idempotency_key="job:123:reserve"),
)
try:
    actual = run_completion()
    result = bursar.credits.settle(
        user_id,
        lease.lease_id,
        actual,
        SettleOptions(idempotency_key="job:123:settle"),
    )
except Exception:
    bursar.credits.release(user_id, lease.lease_id)
    raise
```

```ts
const lease = await bursar.credits.reserve(userId, estimateMetrics, {
  ttl: 120,
  idempotencyKey: "job:123:reserve",
});
try {
  const actual = await runCompletion();
  const result = await bursar.credits.settle(userId, lease.leaseId, actual, {
    idempotencyKey: "job:123:settle",
  });
} catch (err) {
  await bursar.credits.release(userId, lease.leaseId);
  throw err;
}
```

One-call shortcut: `run_billed(user_id, RunBilledOptions(estimate=..., do_work=...,
operation_key="job:123"))` / `runBilled(userId, { estimate, doWork,
operationKey: "job:123" })` — auto-releases on `do_work`
exception, retries settlement. Extend long jobs with `renew(user_id, lease_id,
ttl=300)` / `renew(userId, leaseId, 300)`. Financial-safety presets
(`policy="strict_prepaid"` default, `"overdraft"`) set floors; plans can
reference `prepaid`/`credit_line` policies.

### Check balance and entitlement

```python
balance = bursar.credits.get_balance(user_id)      # locked canonical balance
available = bursar.credits.get_available(user_id)  # balance - active holds (advisory)
allowance = bursar.credits.check_allowance(user_id)  # plan_id, allowance_remaining, period
feature = bursar.credits.check_feature(user_id, "voice_mode")
print(balance.balance, available.available, allowance.allowance_remaining, feature.has_feature)
```

```ts
const balance = await bursar.credits.getBalance(userId);
const available = await bursar.credits.getAvailable(userId);
const allowance = await bursar.credits.checkAllowance(userId);
const feature = await bursar.credits.checkFeature(userId, "voice_mode");
```

Bucket tiers: `get_bucket_balances` / `getBucketBalances`. Advisory UI gating:
`can_afford(user_id, metrics, CanAffordOptions())` / `canAfford(...)` never
raises and is never authoritative. The ledger is auditable via
`list_ledger_entries(user_id, limit=50, cursor=...)` /
`listLedgerEntries(userId, { limit: 50, cursor })` (stable pages, cursor
pagination only); `list_usage_charges` lists metered charges. Refunds and
expiry: `refund_credits(entry_id, amount=None, reason=None, ...)` /
`refundCredits(entryId, { amount?, reason?, ... })`,
`sweep_expired_credits(dry_run=True)` / `sweepExpiredCredits(true)`, and
`revoke_credits_by_entry_type(user_id, "purchase")` /
`revokeCreditsByEntryType(userId, "purchase")`.

### Plan changes

```python
bursar.accounts.on_account_created(user_id, "signup:1")   # default plan + signup grants
bursar.credits.set_user_plan(user_id, "pro")              # emits credits.plan_changed
bursar.credits.unset_user_plan(user_id)
```

```ts
await bursar.accounts.onAccountCreated({
  accountId: userId,
  eventKey: "signup:1",
});
await bursar.credits.setUserPlan(userId, "pro");
await bursar.credits.unsetUserPlan(userId);
```

Subscription cycle credits are webhook-safe and idempotent:
`grant_subscription_cycle(user_id, Decimal("50000"), GrantSubscriptionCycleOptions(bucket="purchased", plan_key="pro", idempotency_key="evt_renewal_1"))`
/ `grantSubscriptionCycle(userId, new Decimal(50000), { bucket: "purchased", planKey: "pro", idempotencyKey: "evt_renewal_1" })`.

### Billing events (webhooks → canonical mutations)

Provider adapters (Stripe, Dodo) map webhooks to normalized `BillingEvent`s;
the facade claims each event on `(provider, event_id, event_type)` so
redelivery never double-posts. Without a `billing_store`, `billing` is
`None` and this raises.

```python
from bursar.billing.types import BillingEvent, BillingEventType, BillingSubscriptionInfo, ProviderRef

bursar.ingest_billing_event(
    BillingEvent(
        provider="stripe",
        event_id="evt_1Paid",
        event_type=BillingEventType.subscription_created,
        occurred_at=datetime.now(UTC).isoformat(),
        user_id=user_id,
        subscription=BillingSubscriptionInfo(
            provider_subscription_id="sub_123",
            status="active",
            refs=ProviderRef(price_id="price_pro_monthly"),
            interval="month",
            interval_count=1,
        ),
    )
)
```

```ts
await bursar.ingestBillingEvent({
  provider: "stripe",
  eventId: "evt_1Paid",
  eventType: BillingEventType.subscription_created,
  occurredAt: new Date().toISOString(),
  userId,
  subscription: {
    providerSubscriptionId: "sub_123",
    status: "active",
    refs: { priceId: "price_pro_monthly" },
    interval: "month",
    intervalCount: 1,
  },
});
```

Related commerce calls (facade `commerce`): `create_checkout(CreateCheckoutInput(...))`
/ `createCheckout(...)` returns the provider session URL; `handle_webhook(raw_body=...,
headers=..., provider=...)` / `handleWebhook(...)`; `auto_recharge.enable(...)` /
`process_if_needed(...)` / `get_status(user_id)` / `disable(...)`;
`preview_plan_change(user_id, offer_key=...)` + `confirm_plan_change(user_id,
operation_key=..., quote_fingerprint=...)` for quote-checked plan changes;
`billing.resolve_offer(provider, product_id=None, price_id=None)` /
`resolve_topup(...)` map provider objects to configured offers.

## Where to go deeper

| Reference                           | Covers                                                                                                                   |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `references/getting-started.md`     | Install, `bursar migrate`, tenant provisioning, facade construction (both SDKs)                                          |
| `references/pricing-config.md`      | Full `BursarConfig` shape: pricing, credits, entitlements, admission, plans, commerce; charge types; expression language |
| `references/credit-lifecycle.md`    | Grants, purchases, deduct, allowances, refunds, expiry sweeps, revocation, ledger/usage reads, idempotency               |
| `references/financial-safety.md`    | Money invariants, leases, `run_billed`, quotas, multitenancy, hard do-nots                                               |
| `references/billing-integration.md` | Offers, checkout, webhooks → `ingest_billing_event`, subscriptions, auto-recharge, plan changes                          |

Live docs (same content, rendered): https://zonastery.github.io/bursar/docs/ —
quickstart, concepts, guides, cli, and the 15-notebook series.
