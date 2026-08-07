# Bursar — The Credit Lifecycle

Every credit that enters or leaves an account posts a canonical ledger
entry. This file explains the ledger model and the money movements, so
metering and charging are implemented correctly.

## Ledger model

- **Accounts** — one personal `credit_accounts` row per subject, plus
  optional `account_kind = 'team'` rows. Each account carries one locked
  canonical balance and a `version` every mutation checks — the hot path
  never re-derives the balance from history.
- **Append-only ledger** — every monetary event is one
  `credit_ledger_entries` row: a signed `amount`, the exact
  `balance_after` after posting, an idempotency key, and a
  `reference_entry_id` for reversals. Entries are never updated or
  deleted; the balance always equals the sum of its entries.
- **Lots and allocations** — every positive entry becomes a `credit_lots`
  row (amount, bucket, priority, expiry). Debits consume lots in priority
  order via `credit_lot_allocations`; refunds restore the source lots the
  original debit consumed, so expiry semantics survive refunds.
- **Buckets** — tiered spend ordering; lower priority numbers spend first
  (`promotional` 1 burns before `purchased` 10).
- **Allowances** — the plan's free credits per window: real money
  _available_ to the account, never in the balance.
- **Exact money** — exact `Decimal` everywhere; the schema stores
  `numeric(20,6)` at 6 dp with `ROUND_HALF_UP`. Never floats.
- **Three money invariants** — one locked balance per account (written
  only by the ledger-posting path, guarded by `version`); balance equals
  the ledger (`balance_after` = account balance + entry amount); the
  ledger is append-only and idempotent (`(account_id, idempotency_key)`
  unique).

## Canonical entry types

Kinds are `grant`, `purchase`, `usage`, `expiry`, `revocation`, `refund`,
`adjustment`, `reservation`, `release`, and `refund_clawback`. Positive
kinds post credits; `usage`, `expiry`, and `reservation` post debits;
`adjustment` can go either way. A refund reverses its source: the `refund`
entry restores the spent balance and a `refund_clawback` entry reverses
the original debits.

## Charging usage

Meter one operation as a `UsageMetrics` and deduct in a single atomic
call (`operation`, `measures` as exact `Decimal` quantities, `dimensions`
select a price rule such as `model`):

```python
from decimal import Decimal
from bursar.metrics import UsageMetrics

metrics = UsageMetrics(
    operation="completion",
    measures={"input_tokens": Decimal(800)},
    dimensions={"model": "gpt-4o"},
)
result = bursar.credits.deduct(ada, metrics, idempotency_key="turn_12345")
print(result.amount, result.allowance_consumed, result.balance_after)
```

```ts
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

`DeductionResult` reports the split: `amount` (net charge after the free
allowance), `allowance_consumed` (covered by the plan's allowance),
`balance_after` (exact balance after the charge).

The cost is priced exactly (no truncation) and charged in one store
transaction that also records usage, enforces entitlement/quota, consumes
allowance, and debits the remainder — all commit or roll back together;
zero-cost usage is still recorded, so quotas cannot be bypassed by a free
rate. Unmetered models are rejected by `unmatched: reject`; replaying
`deduct` with the same `idempotency_key` is a no-op.

- **Flat jobs** — `deduct_flat_job(user_id, job_name, idempotency_key=...)` /
  `deductFlatJob(userId, jobName, { idempotencyKey })` charges a named job's
  fixed cost (`UsageMetrics(operation=job_name,
measures={"jobs": Decimal("1")})`).
- **Feature gates** — pass `feature="voice_mode"` (Python) /
  `feature: "voice_mode"` (TS) to `deduct`/`reserve`/`settle`; the store
  rejects with `FeatureNotEntitledError` when the plan lacks the feature.
- **Raw deductions** — `deduct_credits(user_id, amount, entry_type=...)` /
  `deductCredits(userId, amount, { entryType })` for clawbacks and
  administrative deductions; negative amounts are allow-listed to
  `adjustment`/`refund`.

## Leases: admission for long-running work

When the final cost is only known after the work runs, charge through a
lease: `reserve` prices a worst-case estimate and atomically enforces
entitlement, quota, allowance, and `max_in_flight` (the **only admission
gate**); do the work, calling `renew` before the TTL elapses; then
`settle` bills the actual cost and finalizes, or `release` returns the
hold on failure:

```python
from bursar.credits.service_types import ReserveOptions, SettleOptions

lease = bursar.credits.reserve(
    user_id,
    estimate_metrics,
    ReserveOptions(idempotency_key="job:123:reserve"),
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

The lease captures its `minimum_balance` and pricing snapshot at
admission, so a plan change mid-flight can never strand approved work. A
lease settles exactly once; a settled or released lease cannot be charged
again. `run_billed(user_id, RunBilledOptions(estimate=..., do_work=...,
operation_key="job:123"))` / `runBilled(userId, { estimate, doWork,
operationKey: "job:123" })` wraps reserve → work → settle
in one call (releasing automatically on `do_work` exceptions, retrying
settlement with a bounded attempt count). A crashed worker leaves the
lease to expire: the TTL plus the store's `expire_leases` reaper reclaims
the hold without ever charging it. Exceeding `max_in_flight` raises
`ConcurrencyLimitError` before any hold is taken; settling or renewing an
expired lease raises `LeaseExpiredError`.

## Allowances

A `credit_allowance` grants free credits that reset on a window (the
free plan's monthly 10,000). Windows are `calendar` (timezone-aligned),
`rolling` (duration since first use), or `plan_assignment` (anchored to
plan assignment). Allowance and bucket priorities share one namespace —
lower numbers spend first — and priorities cannot collide. Deductions
draw allowance first, then debit the balance in bucket priority order.

`check_allowance(user_id)` / `checkAllowance(userId)` returns the current
window — `plan_id`/`planId`, `allowance_remaining`/`allowanceRemaining`,
`period_start`/`periodStart`, `period_end`/`periodEnd` — for display and
gating. `set_user_plan(user_id, "pro")` / `setUserPlan(userId, "pro")`
assigns a plan (re-anchoring the allowance window); `unset_user_plan` /
`unsetUserPlan` pauses it.

## Quotas and spend caps

A quota limits one measure of one operation over a window (e.g. the pro
plan's 500,000 `output_tokens`/day). `enforcement` is `block` (the
charge fails with `QuotaExceededError`) or `allow` (usage records, the
balance still pays). `emit_at_percent` fires `credits.quota_threshold`
events (`credits.quota_blocked` when a block fires). Quotas are checked
in the same atomic transaction as the deduction, and reservations count
against them. Read windows with `get_quota_state` / `getQuotaState`,
events with `list_quota_events` / `listQuotaEvents`.

Spend caps are account-plan policy: `deduct_team` / `deductTeam` debits
the team pool with the member as the ledger actor, member spend caps
enforced in the same transaction; a crossing raises `CapReachedError`
(`member_spend_cap_exceeded`), as does a deduction past a configured
`deny` spend cap.

## Expiry and credit tiers

Promotional grants carry an expiry so they must be used first.
`add_credits` accepts `expires_at` (Python) / `expiresAt` (TS) to date a
lot. Inspect what a sweep would expire with
`sweep_expired_credits(dry_run=True)` / `sweepExpiredCredits(true)`, then
run it without the flag; the `SweepResult` reports
`expired_count`/`expiredCount` and `expired_amount`/`expiredAmount`.

Each expired lot posts an `expiry` ledger entry and the amount leaves
the balance — expiry is an accounting event, not a read filter. Run the
sweep on a schedule, or rely on lazy expiry so the user's next operation
clears due lots first. Tiers come from bucket priorities: promotional
credits burn before purchased ones. For fraud/ToS reversals, revoke
whole entry types rather than adjusting by hand:
`revoke_credits_by_entry_type(user_id, "purchase")` /
`revokeCreditsByEntryType(userId, "purchase")` — it walks the `purchase`
lots LIFO across tiers and posts `revocation` ledger entries, so the
audit trail shows exactly what was taken back.

## Refunds

Refund a charge by its entry id; pass `amount` for a partial refund. A
refund never rewrites the original entry — it posts a new `refund`
ledger entry linked back via `reference_entry_id` / `originalEntryId`:

```python
refund = bursar.credits.refund_credits(
    charge.entry_id, amount=0.000030, reason="user_reported_bad_output",
)
print(refund.refund_entry_id, refund.new_balance)
```

```ts
const refund = await bursar.credits.refundCredits(
  charge.entryId,
  {
    amount: new Decimal("0.000030"),
    reason: "user_reported_bad_output",
  },
);
console.log(refund.refundEntryId, refund.newBalance);
```

The store rejects over-refunds, duplicates, and refunds of the wrong
entry type with `RefundError`. Refunds restore the source lots the
original debit consumed, and are idempotent: replaying an omitted-amount
full refund derives a deterministic key from the original entry.

## Reading the ledger

The ledger is the pricing evidence. Walk it with the cursor loop — pages
are stable, ordered by `(created_at, entry_id)` (offset pagination is not
supported):

```python
page = bursar.credits.list_ledger_entries(user_id, limit=50)
while True:
    for entry in page.items:
        print(entry.entry_id, entry.entry_type, entry.amount, entry.created_at)
    if not page.next_cursor:
        break
    page = bursar.credits.list_ledger_entries(user_id, limit=50, cursor=page.next_cursor)
```

```ts
let page = await bursar.credits.listLedgerEntries(userId, { limit: 50 });
while (true) {
  for (const entry of page.items) {
    console.log(entry.entryId, entry.entryType, entry.amount, entry.createdAt);
  }
  if (!page.nextCursor) break;
  page = await bursar.credits.listLedgerEntries(userId, {
    limit: 50,
    cursor: page.nextCursor,
  });
}
```

`list_usage_charges` / `listUsageCharges` is the same loop against
metered usage — each row shows the operation, measures, `charged` amount,
and how much the allowance covered. Balance reads are cheap: `get_balance`
/ `getBalance` (one locked row), `get_available` / `getAvailable`
(balance minus active holds; advisory only — `reserve` is authoritative
for admission), and `get_bucket_balances` / `getBucketBalances` (tiers).
At the end of the day, `get_balance` and the ledger agree: the balance is
the sum of its entries, always.
