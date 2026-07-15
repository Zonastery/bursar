# Red Flags

Security concerns, data integrity risks, silent failures, and identity conflation issues.

## Fix Status

| Finding | Severity | Status |
|---|---|---|
| #8 — Mock declares `provider = "dodo"` | MEDIUM | ✅ Fixed |
| #9 — NULL conflates "not found" vs "permission failure" | MEDIUM | ✅ Fixed |
| #10 — `set_active_bursar_config` swallows billing errors | MEDIUM | ✅ Fixed |
| #33 — `camelToSnake` lowercases all keys | LOW | ✅ Fixed |

**4/4 fixed.**

---

## [MEDIUM] #8 — Mock provider declares `provider = "dodo"` — data indistinguishable from production ✅ Fixed

### Location

`javascript/src/providers/mock/provider.ts:15`

### Code

```typescript
// javascript/src/providers/mock/provider.ts:14-15
export class MockPaymentProvider implements PaymentProvider {
  readonly provider = "dodo" as const;  // ← Mock claims to be Dodo
```

Every billing event emitted by the mock provider is tagged `provider: "dodo"`:

```typescript
// javascript/src/providers/dodo/event-mapper.ts:22
provider: "dodo" as const,
```

This flows through to all persisted rows:
- `billing_events.provider = 'dodo'`
- `billing_subscriptions.provider = 'dodo'`
- `billing_customers.provider = 'dodo'`
- `billing_payments.provider = 'dodo'`

### Impact

When E2E or UI-integration tests run against a real database (not an isolated test DB), mock billing data is indistinguishable from real Dodo billing data. There is no way to:

1. **Filter mock data out of production reports**: `SELECT * FROM billing_subscriptions WHERE provider = 'dodo'` returns both real and mock rows.
2. **Clean up after tests**: A test cleanup query `DELETE FROM billing_subscriptions WHERE provider = 'dodo'` would delete real user subscriptions if run against production by mistake.
3. **Audit**: There's no audit trail distinguishing "a test created this subscription" from "a real Dodo webhook created this subscription".

The zonastery E2E tests use an isolated Supabase instance (port 54421), which mitigates the data-mixing risk. But the UI-integration tests run against the same Supabase as dev (unless explicitly isolated), and the mock data persists.

### Fix

Add a `provider = "mock"` value to the `BillingProvider` type:

```typescript
// javascript/src/billing/billing-types.ts
export type BillingProvider = "stripe" | "dodo" | "mock";
```

```typescript
// javascript/src/providers/mock/provider.ts
export class MockPaymentProvider implements PaymentProvider {
  readonly provider = "mock" as const;  // ← Now distinguishable
```

The `billing_provider_refs` table already uses a `TEXT` column for `provider`, so no SQL migration is needed — `"mock"` will be accepted. The `BillingEventType` enum and all RPCs already use `TEXT` for the provider field.

If backward compatibility with existing mock data is needed, add a migration to update existing rows:

```sql
UPDATE billing_events SET provider = 'mock' WHERE provider = 'dodo' AND payload->>'eventType' LIKE 'mock_%';
-- Or use a metadata flag to identify mock data
```

Alternatively, if the `PaymentProvider` interface constraint (`"stripe" | "dodo"`) must be preserved, add a `isMock: boolean` field to `BillingEvent`:

```typescript
export interface BillingEvent {
  provider: string;  // ← widen to string
  // ...
  isMock?: boolean;  // ← explicit mock flag
}
```

---

## [MEDIUM] #9 — `resolve_billing_offer_by_price` returns NULL for both "not found" AND "permission failure" ✅ Fixed

### Location

`python/src/bursar/sql/013_billing.sql:581-583`

### Code

```sql
-- python/src/bursar/sql/013_billing.sql:575-583
CREATE OR REPLACE FUNCTION public.resolve_billing_offer_by_price(
    p_provider TEXT,
    p_price_id TEXT DEFAULT NULL,
    p_product_id TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_ref RECORD;
    v_offer RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;  -- ← permission failure returns NULL
    END IF;

    -- ... lookup logic ...

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;  -- ← not found also returns NULL
    END IF;
```

### Impact

The `BillingManager` cannot distinguish between:

1. **No offer configured** for this provider's price/product ID — a legitimate "not found" case where the `lookupKey` fallback should apply.
2. **Permission failure** — the caller isn't `service_role`, indicating an RLS misconfiguration or a bug in the service client setup.
3. **Database error** — the RPC threw an error that was caught by the Supabase client and returned as null data.

In all three cases, `resolveBillingOffer` returns `null`, and the `lookupKey` fallback kicks in. If the permission check is failing (e.g. wrong Supabase key used for billing operations), the `lookupKey` fallback will **silently grant the plan from metadata** — bypassing the entire offer resolution and potentially granting access to plans the user didn't purchase.

### Fix

Return an error object instead of NULL for permission failures:

```sql
IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('error', 'unauthorized');
END IF;
```

Then in the store, distinguish error from null:

```typescript
// javascript/src/billing/supabase-billing-store.ts
async resolveBillingOffer(...): Promise<Record<string, unknown> | null> {
  const { data, error } = await this.supabase.rpc("resolve_billing_offer_by_price", {...});
  if (error) throw error;
  if (!data) return null;
  if (data.error === "unauthorized") throw new Error("resolve_billing_offer: unauthorized");
  return this.snakeToCamelKeys(data);
}
```

This ensures permission failures are loud, not silent.

---

## [MEDIUM] #10 — `set_active_bursar_config` swallows billing sync errors ✅ Fixed

### Location

`python/src/bursar/sql/013_billing.sql:816-820`

### Code

```sql
-- python/src/bursar/sql/013_billing.sql:814-820
PERFORM public.sync_plans_from_config(p_config);
PERFORM public.sync_tiers_from_config(p_config);
BEGIN
    PERFORM public.sync_billing_from_config(p_config);
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'billing config sync failed (pricing update still applied): %', SQLERRM;
END;
```

### Impact

When `set_active_bursar_config` is called (e.g. when publishing a new pricing config via the CLI or API), it:
1. Inserts a new `bursar_config` row ✅
2. Syncs plans ✅
3. Syncs tiers ✅
4. Syncs billing offers/topups — **if this fails, it's swallowed as a WARNING**

The pricing config is marked as `active = true` even though the billing offers weren't synced. This means:
- New subscription offers added in the config are NOT in the `billing_offers` table.
- `resolve_billing_offer_by_price` won't find them → `lookupKey` fallback applies → users may get wrong plans.
- The operator sees a successful "pricing published" response, unaware that billing is broken.

### Fix

**Option A — Fail the entire transaction (recommended):**

```sql
PERFORM public.sync_plans_from_config(p_config);
PERFORM public.sync_tiers_from_config(p_config);
PERFORM public.sync_billing_from_config(p_config);
-- If this fails, the entire transaction rolls back, including the pricing config insert.
-- The operator gets an error and knows to fix the billing config before republishing.
```

**Option B — Return a partial success status:**

```sql
DECLARE
    v_billing_error TEXT;
BEGIN
    PERFORM public.sync_plans_from_config(p_config);
    PERFORM public.sync_tiers_from_config(p_config);
    BEGIN
        PERFORM public.sync_billing_from_config(p_config);
    EXCEPTION WHEN OTHERS THEN
        v_billing_error := SQLERRM;
        RAISE WARNING 'billing config sync failed: %', SQLERRM;
    END;

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true,
        'billing_sync_error', v_billing_error  -- ← caller can check this
    );
END;
```

Option B preserves the current behavior but makes the failure visible to the caller, who can then decide whether to alert or retry.

---

## [LOW] #33 — `camelToSnake` in postgres store silently lowercases all keys ✅ Fixed

### Location

`javascript/src/billing/postgres-billing-store.ts:63-79`

### Code

```typescript
// javascript/src/billing/postgres-billing-store.ts:63-79
/**
 * Recursively convert all object keys from camelCase to snake_case.
 *
 * WARNING: this converts ALL keys, including provider names and offer/topup
 * keys. Provider names and config keys MUST be lowercase snake_case (e.g.
 * "stripe", "pro_monthly") — uppercase letters in keys are silently
 * lowercased (e.g. "Stripe" → "stripe", "proMonthly" → "pro_monthly").
 */
private camelToSnake(obj: unknown): unknown {
  if (Array.isArray(obj)) return obj.map((item) => this.camelToSnake(item));
  if (obj && typeof obj === "object" && obj !== null) {
    const converted: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
      const snakeKey = key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
      converted[snakeKey] = this.camelToSnake(value);
    }
    return converted;
  }
  return obj;
}
```

### Impact

The `camelToSnake` function is used in `syncBillingFromConfig` to convert the `BillingConfig` object from camelCase (JS) to snake_case (Postgres JSONB). The conversion is recursive and applied to ALL keys, including:

1. **Offer keys** (`offerKey` → `offer_key`) — correct.
2. **Plan keys** (`planKey` → `plan_key`) — correct.
3. **Provider names** (`"stripe"` → `"stripe"`) — no change, already lowercase.
4. **Provider ref keys** (`productId` → `product_id`) — correct.

But if a user accidentally names an offer `"proMonthly"` (camelCase), it becomes `"pro_monthly"` in the DB. If they later try to resolve it by `"proMonthly"`, it won't match. The warning comment documents this, but it's a silent transformation that could surprise users.

More subtly, the regex `key.replace(/[A-Z]/g, ...)` converts ALL uppercase letters, not just those in camelCase positions. So a key like `"URL"` becomes `"_u_r_l"` — completely wrong. While offer/plan keys are unlikely to contain all-caps, this is a latent bug.

### Fix

Use a proper camelCase-to-snake_case converter that only inserts underscores before uppercase letters that follow lowercase letters or digits:

```typescript
private camelToSnake(str: string): string {
  return str
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")  // fooBar → foo_bar
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")  // FOOBar → FOO_Bar
    .toLowerCase();
}
```

Or use a library like `lodash.snakeCase` or `change-case` which handle edge cases correctly.
