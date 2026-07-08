# Anniversary Reset: Bursar vs. Payment Provider Alignment

## Overview

Audit comparing bursar's `resolveAnniversary` against Stripe and Dodo Payments subscription billing cycle strategies. Initiated because bursar's free-allowance anniversary reset should mirror how payment providers handle subscription renewals.

**Verdict:** The `resolveAnniversary` algorithm is correct and matches both providers. The real gap is `planAssignedAt` anchoring — always set to `now()` instead of the provider's actual period start date.

---

## Payment Provider Strategies

### Stripe: `billing_cycle_anchor`

Stripe controls renewal dates via the **billing cycle anchor**, which sets the first full billing date and all future dates.

**Two approaches:**

1. **`billing_cycle_anchor_config[day_of_month]=N`** (recommended for monthly/yearly):
   - Fixes a day-of-month (e.g., 1st, 15th, 31st) for all customers
   - Stripe auto-handles short months and leap years
   - After a short-month clamp, snap back to the original day in the next month
   - Example: anchor day 31 → Feb 28 → Mar 31 → Apr 30 → May 31

2. **`billing_cycle_anchor` (UNIX timestamp)**:
   - Sets an exact timestamp for the first full billing date
   - Must be within the first billing period

**Proration behavior** controls the initial gap period:
   - `create_prorations` (default): prorated invoice for the gap
   - `none`: initial period is free, first full invoice at anchor

**Limitations:**
   - Can't use with free trials in Checkout
   - `billing_cycle_anchor_config` only works with monthly/yearly intervals
   - `billing_cycle_anchor` and trial settings are mutually exclusive

**Source:** https://docs.stripe.com/payments/checkout/billing-cycle

### Dodo Payments: Proration and Billing Cycle

Dodo determines renewals via subscription creation timestamp and billing interval. The API exposes `previous_billing_date` (period start) and `next_billing_date` (period end).

**Proration modes for plan changes:**

| Mode | Billing Cycle Anchor | Best For |
|---|---|---|
| `prorated_immediately` | Stays same (original renewal preserved) | Upgrades within product family |
| `difference_immediately` | Credit applies to next renewal | Downgrades |
| `full_immediately` | Resets to today (new cycle starts) | Annual-to-monthly switches |

**Key insight:** When `prorated_immediately` is used (the common upgrade path), the original billing cycle anchor is preserved — same as Stripe's fixed `billing_cycle_anchor_config`. The renewal date stays fixed from the original subscription date.

**Testing:** Dodo allows manually setting `next_billing_date` via PATCH API to trigger immediate renewal for testing.

**Source:** https://docs.dodopayments.com/api-reference/subscriptions/patch-subscriptions

### Industry Standard Summary

Both providers follow the same pattern for monthly subscriptions:

1. **Renewal date = fixed day-of-month** (from original subscription or explicit anchor)
2. **Short-month clamp**: day 31 in February → last day of February (28 or 29)
3. **Snap-back**: next month returns to original day (Feb 28 → Mar 31)
4. **Anchor is sticky**: preserved through renewals and prorated upgrades

---

## Bursar's Current Implementation

### Core Logic

**File:** `packages/bursar/javascript/src/allowance.ts:68`

```typescript
function resolveAnniversary(now: Date, anchor: Date | null): { start: Date; end: Date } {
  if (anchor === null) return resolveCalendarMonth(now);
  const anchorMidnight = utcMidnight(anchor);
  const nowMidnight = utcMidnight(now);
  const resetDay = anchorMidnight.getUTCDate();

  // Candidate reset date in the same UTC year/month as `now`, clamped.
  const candidate = clampedDayInMonth(
    nowMidnight.getUTCFullYear(),
    nowMidnight.getUTCMonth(),
    resetDay,
  );

  let start: Date;
  if (candidate.getTime() <= nowMidnight.getTime()) {
    start = candidate;
  } else {
    // The clamped reset date this month is still ahead of `now` — the current
    // window started last month.
    start = clampedDayInMonth(
      nowMidnight.getUTCFullYear(),
      nowMidnight.getUTCMonth() - 1,
      resetDay,
    );
  }

  const end = clampedDayInMonth(start.getUTCFullYear(), start.getUTCMonth() + 1, resetDay);
  return { start, end };
}
```

### Python Parity

**File:** `packages/bursar/python/src/bursar/allowance.py:69`

```python
def _anniversary_window(now: date, anchor_date: date) -> tuple[date, date]:
    anchor_day = anchor_date.day
    this_month_reset = _clamped_day(now.year, now.month, anchor_day)

    if now >= this_month_reset:
        start = this_month_reset
        end_year, end_month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        end = _clamped_day(end_year, end_month, anchor_day)
    else:
        prev_year, prev_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = _clamped_day(prev_year, prev_month, anchor_day)
        end = this_month_reset

    return start, end
```

Functionally identical. Both strip time-of-day, clamp to month length, and snap back after short months.

### Algorithm: Step by Step

Given anchor day-of-month `D` and current date `now`:

1. Build `candidate = clamp(now.year, now.month, D)` — the reset date in `now`'s month
2. If `candidate <= now`: window starts at `candidate`, ends `+1 month` (clamped)
3. If `candidate > now`: window started previous month (clamped), ends at `candidate`
4. Return `{ start, end }` — both exclusive-end convention

### Edge Case: Short-Month Clamp (Anchor Day 31)

Trace for anchor day 31, non-leap year:

| now | candidate | candidate ≤ now? | start | end |
|---|---|---|---|---|
| Feb 15 | Feb 28 | No | Jan 31 | Feb 28 |
| Mar 1 | Mar 31 | No | Feb 28 | Mar 31 |
| Apr 1 | Apr 30 | No | Mar 31 | Apr 30 |
| May 1 | May 31 | Yes | May 31 | Jun 30 |

Key property: **Snap-back after clamp.** Feb 28 window ends at Mar 31 (not Mar 28). This matches Stripe/Dodo behavior.

### Edge Cases Covered

| Case | Test File | Lines |
|---|---|---|
| Null anchor → calendar_month fallback | both | Py 112, JS 97 |
| Normal day-of-month reset | both | Py 117, JS 105 |
| Before reset day → previous window | both | Py 123, JS 112 |
| Anchor day 31, non-leap Feb (28) | both | Py 135, JS 119 |
| Anchor day 31, leap Feb (29) | both | Py 141, JS 134 |
| Anchor day 30, Feb clamp (28) | both | Py 173, JS 145 |
| Year boundary (Dec → Jan) | py only | Py 179 |
| Time-of-day discarded | py only | Py 185 |
| UTC-only (no TZ dependence) | js only | JS 157 |

---

## The Real Gap: `planAssignedAt` Anchoring

### How the Anchor Gets Set

Every time `setUserPlan()` is called, `plan_assigned_at = now()` — the server timestamp at webhook receipt:

| Code Path | File | Value |
|---|---|---|
| SQL RPC | `packages/bursar/python/src/bursar/sql/004_plans.sql:231` | `now()` |
| Python MemoryStore | `python/src/bursar/interface/memory.py:1272` | `self._utcnow()` |
| JS MemoryStore | `javascript/src/stores/memory-store.ts:1353` | `this.now()` |
| JS PostgresStore | `javascript/src/stores/postgres-store.ts:655` | `set_user_plan` RPC → `now()` |
| JS SupabaseStore | `javascript/src/stores/supabase-store.ts:669` | `set_user_plan` RPC → `now()` |

### Trigger Path from Webhooks

`ensureUserPlan(userId, planSlug)` is called from:

1. **`web/lib/payment/shared-webhook.ts`** — `handleSubscriptionEvent()`:
   - Called by Dodo's `onSubscriptionActive`, `onSubscriptionRenewed`, `onSubscriptionPlanChanged`
   - Called by Stripe's `checkout.session.completed` and `invoice.paid`
2. **`web/lib/payment/dodo/index.ts:206`** — `onSubscriptionPlanChanged` handler

In all cases, `plan_assigned_at = now()`, not the provider-reported period start.

### Payment Provider Dates Are Tracked Separately

The `subscriptions` table stores provider dates in `current_period_end`, but this is never passed to bursar:

| Data Point | Table | Set by | Value |
|---|---|---|---|
| `plan_assigned_at` | `user_credits` | `set_user_plan` RPC | Always `now()` |
| `current_period_end` | `subscriptions` | `upsert_subscription` webhook handler | From provider (`next_billing_date` / Stripe `current_period_end`) |

The two tables are never cross-referenced for anchoring.

### Impact Assessment

| Allowance Period | Impact | Rationale |
|---|---|---|
| `calendar_month` | None | Ignores anchor entirely |
| `anniversary` | Negligible | Only day-of-month matters; webhook fires same day as renewal |
| `rolling_30d` | Very low | Day-granularity `utcMidnight` masks sub-day drift |

**Proration edge case:** If a user subscribes Jan 31 and the provider sets billing anchor to Feb 1 via proration, `plan_assigned_at` will be Jan 31 (webhook time), but subscription renews Feb 1. Bursar resets on 31st; provider bills on 1st. One-day misalignment. Rare — most initial subscriptions without trials don't use prorated anchors.

---

## Proposed Fix: Sync Anchor with Provider Period Start

If precision matters in the future (e.g., strict `rolling_30d` alignment), the anchoring chain needs to accept an optional `planAssignedAt` override.

### Files to Modify

**Bursar (JS) — thread optional anchor through the stack:**

| File | Change |
|---|---|
| `javascript/src/stores/credit-store.ts` | `setUserPlan`: add `planAssignedAt?: Date` param |
| `javascript/src/stores/memory-store.ts` | Pass `planAssignedAt` through |
| `javascript/src/stores/postgres-store.ts` | Pass as RPC param (SQL change needed) |
| `javascript/src/stores/supabase-store.ts` | Pass as RPC param |
| `javascript/src/manager.ts` | `setUserPlan`: thread optional anchor |

**Bursar (Python) — same threading:**

| File | Change |
|---|---|
| `python/src/bursar/sql/004_plans.sql` | `set_user_plan` RPC: accept optional `p_plan_assigned_at` |
| `python/src/bursar/interface/memory.py` | `set_user_plan`: accept optional anchor |
| `python/src/bursar/interface/postgres.py` | `set_user_plan`: pass through |
| `python/src/bursar/interface/supabase.py` | `set_user_plan`: pass through |

**Web (consumers) — extract and forward provider dates:**

| File | Change |
|---|---|
| `web/lib/credit/billing.ts` | `ensureUserPlan`: accept optional anchor, forward to `cm.setUserPlan` |
| `web/lib/payment/shared-webhook.ts` | Extract `previous_billing_date` from Dodo payload / Stripe `current_period_start`, pass to `ensureUserPlan` |
| `web/lib/payment/dodo/index.ts` | Surface `previous_billing_date` in webhook data |
| `web/lib/payment/stripe/webhook.ts` | Surface `current_period_start` |

### Verification

```bash
# Bursar tests (algorithm unchanged — should pass)
cd packages/bursar/javascript && npm test
cd packages/bursar/python && uv run pytest

# Webhook handler unit tests
cd web && npx vitest run lib/payment/__tests__/

# E2E: subscribe, verify plan_assigned_at matches provider's previous_billing_date
cd web && npx playwright test e2e/specs/subscription-lifecycle.spec.ts
```

---

## References

- Bursar JS: `packages/bursar/javascript/src/allowance.ts`
- Bursar Python: `packages/bursar/python/src/bursar/allowance.py`
- Bursar SQL: `packages/bursar/python/src/bursar/sql/004_plans.sql`
- Bursar tests (JS): `packages/bursar/javascript/tests/allowance.test.ts`
- Bursar tests (Python): `packages/bursar/python/tests/test_allowance.py`
- Manager memory store (JS): `packages/bursar/javascript/src/stores/memory-store.ts`
- Manager memory store (Python): `packages/bursar/python/src/bursar/interface/memory.py`
- Stripe billing cycle docs: https://docs.stripe.com/payments/checkout/billing-cycle
- Dodo subscription API: https://docs.dodopayments.com/api-reference/subscriptions/patch-subscriptions
- Dodo proration guide: https://dodopayments.com/blogs/subscription-upgrade-downgrade-proration
- Webhook handler: `web/lib/payment/shared-webhook.ts`
- Dodo webhook handler: `web/lib/payment/dodo/index.ts`
- Stripe webhook handler: `web/lib/stripe/webhook.ts`
- Billing integration: `web/lib/credit/billing.ts`
- Bursar client: `web/lib/credit/bursar-client.ts`
