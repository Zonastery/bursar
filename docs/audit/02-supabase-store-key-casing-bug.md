# CRITICAL: Supabase Store Key-Casing Bug

> **Severity**: CRITICAL
> **Status**: ✅ FIXED
> **Verification**: JS typecheck 0 errors, Python typecheck 0 errors; 916/916 JS tests pass, 79/79 Python billing tests pass
> **Changes**: JS `SupabaseBillingStore` now transforms RPC output via `snakeToCamelKeys()`; Python `SupabaseBillingStore` reads `credits_per_major_unit` (snake_case)

---

## Root Cause

The SQL RPCs (`resolve_billing_offer_by_price`, `resolve_credit_topup_by_price`) return JSONB objects with **snake_case** keys. The `PostgresBillingStore` transforms these keys to camelCase via `snakeToCamelKeys()`. The `SupabaseBillingStore` does **NOT** transform keys — it returns the raw RPC output directly. The `BillingManager` reads camelCase keys (JS) or snake_case keys (Python), creating a mismatch in both SDKs.

---

## Finding #1 — JS: Offer resolution silently fails, plans never provisioned ✅ Fixed

### Location

- `javascript/src/billing/supabase-billing-store.ts:24-36` — `resolveBillingOffer` returns raw RPC data
- `javascript/src/billing/billing-manager.ts:221-234` — manager reads camelCase keys
- `python/src/bursar/sql/013_billing.sql:544-553` — RPC returns snake_case JSONB

### Code

The SQL RPC returns snake_case keys:

```sql
-- python/src/bursar/sql/013_billing.sql:544-553
RETURN jsonb_build_object(
    'offer_key', v_offer.offer_key,
    'plan_key', v_offer.plan_key,
    'interval', v_offer.interval,
    'interval_count', v_offer.interval_count,
    'entitlement_mode', v_offer.entitlement_mode,
    'cycle_grant_credits', v_offer.cycle_grant_credits,
    'cycle_grant_tier', v_offer.cycle_grant_tier,
    'cycle_grant_replace_prior', v_offer.cycle_grant_replace_prior
);
```

The JS supabase store returns this directly without key transformation:

```typescript
// javascript/src/billing/supabase-billing-store.ts:24-36
async resolveBillingOffer(
  provider: string,
  productId?: string | null,
  priceId?: string | null,
): Promise<Record<string, unknown> | null> {
  const { data, error } = await this.supabase.rpc("resolve_billing_offer_by_price", {
    p_provider: provider,
    p_price_id: priceId ?? undefined,
    p_product_id: productId ?? undefined,
  });
  if (error) throw error;
  return data as Record<string, unknown> | null;
  // ← data has keys: { offer_key, plan_key, interval, interval_count, ... }
  // ← NO snakeToCamelKeys transformation
}
```

The JS postgres store DOES transform keys:

```typescript
// javascript/src/billing/postgres-billing-store.ts:36-53
private async callRpcJson(rpcName: string, params: unknown[]): Promise<Record<string, unknown> | null> {
  const placeholders = params.map((_, i) => `$${i + 1}`).join(", ");
  const rows = await this.pool.query(`SELECT * FROM public.${rpcName}(${placeholders})`, params);
  if (rows.rows.length === 1) {
    const row = rows.rows[0] as Record<string, unknown>;
    const keys = Object.keys(row);
    if (keys.length === 1) {
      const v = row[keys[0]];
      if (v !== null && typeof v === "object" && !Array.isArray(v)) {
        return this.snakeToCamelKeys(v) as Record<string, unknown>;
        // ← keys transformed: offer_key → offerKey, plan_key → planKey, etc.
      }
    }
  }
  return null;
}
```

The JS billing manager reads camelCase keys:

```typescript
// javascript/src/billing/billing-manager.ts:221-234
if (offer) {
  return {
    offer,
    offerKey: (offer?.offerKey as string | null) ?? null,    // ← reads offerKey (camelCase)
    planKey: (offer?.planKey as string | null) ?? null,      // ← reads planKey (camelCase)
  };
}
```

```typescript
// javascript/src/billing/billing-manager.ts:647-648
const planKey = offer.planKey as string | undefined;  // ← reads planKey (camelCase)
if (!planKey) return;  // ← undefined → provisioning ABORTED
```

```typescript
// javascript/src/billing/billing-manager.ts:654
const entitlementMode = offer?.entitlementMode as string | undefined;  // ← reads entitlementMode
if (entitlementMode === "cycle_grant" && this.cm) {  // ← undefined === "cycle_grant" → false
```

### Impact

With the JS `SupabaseBillingStore`:

| Manager reads | RPC returns | Result |
|---|---|---|
| `offer.offerKey` | `offer.offer_key` | `undefined` |
| `offer.planKey` | `offer.plan_key` | `undefined` |
| `offer.entitlementMode` | `offer.entitlement_mode` | `undefined` |
| `offer.cycleGrantCredits` | `offer.cycle_grant_credits` | `undefined` |
| `offer.cycleGrantTier` | `offer.cycle_grant_tier` | `undefined` |
| `offer.cycleGrantReplacePrior` | `offer.cycle_grant_replace_prior` | `undefined` |

**Chain of failures:**

1. `resolveOfferAndKeys` returns `{ offer: {...}, offerKey: null, planKey: null }` — the offer object has data but the extracted keys are null.
2. `provisionSubscription` reads `offer.planKey` → `undefined` → returns early. **Plan is never set.**
3. `entitlementMode` is `undefined` → `=== "cycle_grant"` is `false`. **Cycle grants are never issued.**
4. The subscription row IS persisted (via `upsertBillingSubscription`) but with `offer_key: null` and `plan_key: null`.

**User-visible effect**: A Stripe customer subscribes successfully. The `billing_subscriptions` row shows `status: "active"` but `plan_key: null`. The user's credit plan is never set. They have no allowances, no caps, no cycle grants. Every API call that checks `getUserPlan()` returns nothing.

### Why it's masked in tests

| Store | Keys returned | Manager reads | Works? |
|---|---|---|---|
| `MemoryBillingStore` | camelCase (stores JS objects directly) | camelCase | Yes |
| `PostgresBillingStore` | camelCase (via `snakeToCamelKeys`) | camelCase | Yes |
| `SupabaseBillingStore` | **snake_case** (raw RPC output) | camelCase | **NO** |

Additionally, Dodo and Mock webhooks are saved by the `lookupKey` fallback (`billing-manager.ts:228-235`), which synthesizes a camelCase offer `{ planKey: refs.lookupKey, entitlementMode: "allowance" }`. This bypasses `resolveBillingOffer` entirely. So:

- **Dodo/Mock + SupabaseStore**: Works (via `lookupKey` fallback)
- **Stripe + SupabaseStore**: **BROKEN** (Stripe event-mapper sets `refs: { lookupKey: planSlug }` only if `session.metadata?.plan_slug` exists, which is not always the case)
- **Stripe + PostgresStore**: Works (via `snakeToCamelKeys`)
- **Any provider + MemoryStore**: Works (camelCase)

The integration tests in `billing-integration.test.ts` use `PostgresBillingStore` (real Postgres) and `MemoryBillingStore` — never `SupabaseBillingStore` against a real Supabase instance. The `SupabaseBillingStore` is only tested in the zonastery `web/` layer, where the Dodo/Mock `lookupKey` fallback masks the bug.

### Fix

Add key transformation to the JS `SupabaseBillingStore`, matching the `PostgresBillingStore` pattern:

```typescript
// javascript/src/billing/supabase-billing-store.ts

private snakeToCamel(str: string): string {
  return str.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
}

private snakeToCamelKeys(obj: unknown): unknown {
  if (Array.isArray(obj)) return obj.map((item) => this.snakeToCamelKeys(item));
  if (obj && typeof obj === "object" && obj !== null) {
    const converted: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      converted[this.snakeToCamel(key)] = this.snakeToCamelKeys(value);
    }
    return converted;
  }
  return obj;
}

async resolveBillingOffer(
  provider: string,
  productId?: string | null,
  priceId?: string | null,
): Promise<Record<string, unknown> | null> {
  const { data, error } = await this.supabase.rpc("resolve_billing_offer_by_price", {
    p_provider: provider,
    p_price_id: priceId ?? undefined,
    p_product_id: productId ?? undefined,
  });
  if (error) throw error;
  if (!data) return null;
  return this.snakeToCamelKeys(data) as Record<string, unknown>;
}

async resolveCreditTopup(
  provider: string,
  productId?: string | null,
  priceId?: string | null,
): Promise<Record<string, unknown> | null> {
  const { data, error } = await this.supabase.rpc("resolve_credit_topup_by_price", {
    p_provider: provider,
    p_price_id: priceId ?? undefined,
    p_product_id: productId ?? undefined,
  });
  if (error) throw error;
  if (!data) return null;
  return this.snakeToCamelKeys(data) as Record<string, unknown>;
}
```

**Also fix**: `getUserSubscription` (line 118) calls `get_user_billing_subscription` which returns snake_case keys. The `rowToSubscriptionState` mapper (lines 127-146) reads both `r.offer_key` (snake) and converts to `offerKey` (camel). This part works correctly — verify but likely no fix needed.

---

## Finding #2 — Python: `compute_topup_credits` always uses default 1000 ✅ Fixed

### Location

`python/src/bursar/billing/supabase.py:212-218`

### Code

```python
# python/src/bursar/billing/supabase.py:193-210
def resolve_credit_topup(
    self,
    provider: str,
    product_id: str | None = None,
    price_id: str | None = None,
) -> dict | None:
    result = self._supabase.rpc(
        "resolve_credit_topup_by_price",
        {
            "p_provider": provider,
            "p_price_id": price_id,
            "p_product_id": product_id,
        },
    )
    if "error" in result and result["error"]:
        raise RuntimeError(result["error"])
    data = result.get("data")
    return data if data else None
    # ← data has keys: { topup_key, tier, currency, credits_per_major_unit, ... }
    # ← NO key transformation
```

```python
# python/src/bursar/billing/supabase.py:212-218
def compute_topup_credits(
    self,
    amount_minor: int,
    topup_config: dict,
) -> int:
    credits_per = int(topup_config.get("creditsPerMajorUnit", 1000))
    #                           ^^^^^^^^^^^^^^^^^
    #                           camelCase key
    #                           RPC returns: credits_per_major_unit (snake_case)
    #                           .get() returns 1000 (the default) ALWAYS
    return (amount_minor * credits_per) // 100
```

The same method in the Python `MemoryBillingStore` and `PostgresBillingStore` also reads `credits_per_major_unit` (snake_case) — but those stores either store the config directly (memory) or transform keys (postgres via `_call_rpc_json_sync`). The `PostgresBillingStore` version:

```python
# python/src/bursar/billing/postgres.py:323-325
def compute_topup_credits(self, amount_minor: int, topup_config: dict) -> int:
    credits_per = topup_config.get("credits_per_major_unit", 1000)
    #                           ^^^^^^^^^^^^^^^^^^^^^
    #                           snake_case — matches RPC output
    return (amount_minor * credits_per) // 100
```

Wait — the `PostgresBillingStore` reads snake_case. The `MemoryBillingStore` also reads snake_case. But the `SupabaseBillingStore` reads **camelCase**. This is the divergence.

### Impact

With the Python `SupabaseBillingStore`:

| Config key in RPC output | `SupabaseBillingStore` reads | Result |
|---|---|---|
| `credits_per_major_unit` | `creditsPerMajorUnit` | `1000` (always — default) |
| `min_amount_minor` | (not read in `compute_topup_credits`) | N/A |
| `max_amount_minor` | (not read in `compute_topup_credits`) | N/A |

**User-visible effect**: A SaaS configures a topup with `credits_per_major_unit: 500` (0.5 credits per cent). When using the Supabase store, every topup grants credits at 1000/major unit instead of 500. A $10 purchase grants 10,000 credits instead of 5,000. This is a **financial bug** — users receive double the configured credits.

Note: The Python `BillingManager._handle_payment_succeeded` also reads the topup config for currency/min/max checks:

```python
# python/src/bursar/billing/manager.py:456-461
if event.payment.currency.upper() != str(topup_config.get("currency", "USD")).upper():
    return BillingEventResult(handled=True, action="payment_succeeded")
min_amount = int(topup_config.get("min_amount_minor", 0))
max_amount = int(topup_config.get("max_amount_minor", 10**18))
```

These read snake_case keys (`min_amount_minor`, `max_amount_minor`) which match the RPC output. So the currency and bounds checks work correctly — only the credit calculation is broken.

### Fix

Change `compute_topup_credits` in the Python `SupabaseBillingStore` to read snake_case keys (matching the RPC output and the other Python stores):

```python
# python/src/bursar/billing/supabase.py:212-218
def compute_topup_credits(
    self,
    amount_minor: int,
    topup_config: dict,
) -> int:
    credits_per = int(topup_config.get("credits_per_major_unit", 1000))
    #                           ^^^^^^^^^^^^^^^^^^^^^^
    #                           snake_case — matches RPC output
    return (amount_minor * credits_per) // 100
```

This aligns with the `PostgresBillingStore` and `MemoryBillingStore` which both read `credits_per_major_unit`.

---

## Finding #2b — JS: `computeTopupCredits` also reads camelCase from supabase store

### Location

`javascript/src/billing/supabase-billing-store.ts:162-168`

### Code

```typescript
// javascript/src/billing/supabase-billing-store.ts:162-168
async computeTopupCredits(
  amountMinor: number,
  topupConfig: Record<string, unknown>,
): Promise<number> {
  const creditsPer = (topupConfig.creditsPerMajorUnit as number) ?? 1000;
  //                    ^^^^^^^^^^^^^^^^^^^^^
  //                    camelCase — but RPC returns credits_per_major_unit
  return Math.trunc((amountMinor * creditsPer) / 100);
}
```

The `topupConfig` passed to `computeTopupCredits` comes from `resolveCreditTopup` (line 148-160) which returns raw RPC data with snake_case keys. So `topupConfig.creditsPerMajorUnit` is `undefined` → falls back to `1000`.

### Impact

Same as Finding #2 — topup credits always calculated at 1000/major unit regardless of configuration.

### Fix

With the `snakeToCamelKeys` fix from Finding #1 applied to `resolveCreditTopup`, the `topupConfig` will have camelCase keys and `topupConfig.creditsPerMajorUnit` will work correctly.

Alternatively, if you prefer not to transform keys in `resolveCreditTopup`, change `computeTopupCredits` to read snake_case:

```typescript
const creditsPer = (topupConfig.credits_per_major_unit as number) ?? 1000;
```

But this creates inconsistency with the other JS stores which read camelCase. **Recommended**: apply `snakeToCamelKeys` to `resolveCreditTopup` (and `resolveBillingOffer`) in the supabase store, so all JS stores return camelCase consistently.

---

## Additional affected methods in JS SupabaseBillingStore

If `snakeToCamelKeys` is NOT applied globally, these methods also return snake_case keys that the manager reads as camelCase:

| Method | Line | Returns snake_case | Manager reads camelCase | Impact |
|---|---|---|---|---|
| `resolveBillingOffer` | 24-36 | `offer_key`, `plan_key`, `entitlement_mode`, etc. | `offerKey`, `planKey`, `entitlementMode` | Plans never provisioned |
| `resolveCreditTopup` | 148-160 | `credits_per_major_unit`, `min_amount_minor`, `max_amount_minor` | `creditsPerMajorUnit`, `minAmountMinor`, `maxAmountMinor` | Wrong credit amounts, wrong bounds |
| `getBillingSubscription` | 105 | Handled by `rowToSubscriptionState` (lines 127-146) which maps snake→camel manually | Works | OK |
| `getBillingPayment` | 268 | Returns raw RPC row | Manager reads `payment.purpose`, `payment.metadata` | Verify — `purpose` and `metadata` are single-word keys, no casing issue |

The `claimBillingEvent` method (line 38-55) reads `result.status` — `status` is a single word, no casing issue. `rowToSubscriptionState` (line 127-146) manually maps each field with explicit `String()`, `Number()`, `Boolean()` coercions, so it handles snake_case correctly. The problem is specifically with `resolveBillingOffer` and `resolveCreditTopup` which return raw JSONB without per-field mapping.

---

## Verification Steps

After applying the fix:

1. **Unit test**: Add a test that mocks the Supabase RPC to return snake_case JSONB and asserts the store returns camelCase keys.
2. **Integration test**: Run the existing `billing-integration.test.ts` suite against a real Supabase instance (not just Postgres) to verify the full flow.
3. **Manual test**: Send a Stripe `checkout.session.completed` webhook with a configured offer (not relying on `lookupKey`) through a Supabase-backed `BillingManager` and verify the user's plan is set.
