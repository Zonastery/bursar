# 01 — Critical Fixes (C1, C2, C3)

## C1 — Python billing sync to SQL is broken

### Evidence
- `python/src/bursar/billing/postgres.py:63-65` and
  `python/src/bursar/billing/supabase.py:18-22` pass `config.model_dump()`
  (new shape: `subscriptions`/`topups` with `plan`, `grant.*`, `providers`,
  `deposit_to`, `credits_per_unit`) directly to the `sync_billing_from_config`
  SQL RPC.
- `python/src/bursar/sql/013_billing.sql:554-643` still reads OLD keys:
  `v_item->>'plan_key'`, `v_item->>'entitlement_mode'`,
  `v_item->>'cycle_grant_credits'`, `v_item->>'cycle_grant_tier'`,
  `v_item->>'cycle_grant_replace_prior'`, `v_item->'provider_refs'`,
  `p_config->'credit_topups'`, `v_item->>'tier'`,
  `v_item->>'credits_per_major_unit'`.

### Impact
- **Subscriptions:** `billing_offers.plan_key` is `NOT NULL` (`013:11`);
  `v_item->>'plan_key'` is NULL for the new config → NOT NULL constraint
  violation. The direct call from `BillingManager.__init__`
  (`billing/manager.py:90-91`) **raises**; `setup_pricing` swallows it as a
  WARNING (`013:1218-1222`).
- **Topups:** SQL reads `p_config->'credit_topups'` (`013:616`); Python sends
  `topups` → `IF` guard false → topups **silently never synced**.
- JS does **not** have this bug: `javascript/src/billing/postgres-billing-store.ts:84-135`
  and `supabase-billing-store.ts:54-81` explicitly adapt new→old.

### Why uncaught
`python/tests/test_billing_manager.py:673` and
`python/tests/test_billing_integration.py:124` both use `MemoryBillingStore`.
No real-DB test exercises Python `PostgresStore`/`SupabaseStore` billing sync.

### Fix (under full-rename end state)
With the SQL fully renamed (see `02-sql-rename.md`), `sync_billing_from_config`
reads the new BillingConfig shape natively, so **no adaptation is needed**:
- `billing/postgres.py:63-65` — keep `config.model_dump()`, pass directly.
  No code change beyond confirming the SQL reads the dumped keys.
- `billing/supabase.py:18-22` — same.
- Add the real-DB billing-sync test (see `05-tests-parity.md`).
- JS adaptation layer (`postgres-billing-store.ts:79-135`,
  `supabase-billing-store.ts:54-81`) is **deleted** in Step 5.

---

## C2 — `setup_pricing` billing sync is a silent no-op for new configs

### Evidence
`setup_pricing` (`013_billing.sql:1216-1222`) passes the full pricing config
`p_config` to `sync_billing_from_config`, which reads top-level `subscriptions`
(`013:554`) and `credit_topups` (`013:616`). New configs nest these under
`billing` (`billing.subscriptions` / `billing.topups`), so both `IF` guards are
false and nothing syncs.

### Impact
Publishing a new pricing config via `setup_pricing` syncs **zero** billing
offers/topups through that path. Billing only syncs via the `BillingManager`
direct call (broken in Python per C1; works in JS).

### Fix
- `013_billing.sql:1219` — change
  `PERFORM public.sync_billing_from_config(p_config);` →
  `PERFORM public.sync_billing_from_config(p_config->'billing');`
- `sync_billing_from_config` then reads a BillingConfig-shaped argument
  (top-level `subscriptions`/`topups`), matching what Python
  `config.model_dump()` and the JS `config` object produce.
- Decide on the `EXCEPTION WHEN OTHERS` guard (`013:1218-1222`): once sync is
  correct, prefer to **remove** it so errors surface instead of being swallowed
  as warnings.

---

## C3 — `grant` is not a discriminated union

### Evidence
`python/src/bursar/billing/models.py:155-161`:
```python
class SubscriptionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["allowance", "cycle_grant"] = "allowance"
    credits: int | None = None
    bucket: str = "purchased"
    replace_prior: bool = True
```
This is a single flat model with conditionally-live fields — exactly the pattern
the revamp design principle 2.3 says to eliminate. It is **not** an
`Annotated[Union, Field(discriminator="mode")]`.

### Impact
- `{mode: cycle_grant}` without `credits` is **accepted** (spec requires
  `credits` for `cycle_grant`).
- `{mode: allowance, credits: 500, bucket: "x"}` is **accepted** (spec says
  allowance mode carries only `{mode: allowance}`).
- "Make impossible states unrepresentable" is not achieved.

### Fix
**Python** (`billing/models.py`):
```python
class AllowanceGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["allowance"] = "allowance"

class CycleGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["cycle_grant"] = "cycle_grant"
    credits: int = Field(ge=0)
    bucket: str = "purchased"
    replace_prior: bool = True

Grant = Annotated[AllowanceGrant | CycleGrant, Field(discriminator="mode")]
```
- `BillingOffer.grant: Grant = Field(default_factory=lambda: AllowanceGrant())`
  (`billing/models.py:170`).
- `BillingConfig.from_bursar_config` (`billing/models.py:192-215`) already
  forwards `billing_data.subscriptions` values; confirm dict→model construction
  uses the union (Pydantic discriminated union validates the `mode` tag).

**JavaScript** (`javascript/src/billing/billing-types.ts`): mirror the union
(`AllowanceGrant | CycleGrant` discriminated on `mode`); `BillingOffer.grant`
typed as the union.

**SQL:** no union — flat renamed columns `grant_mode`, `grant_credits`,
`grant_bucket`, `grant_replace_prior` (see `02-sql-rename.md`). The store maps
union ↔ flat columns at the boundary.

**Parity fixture:** add cases (see `05-tests-parity.md`):
- `{grant: {mode: cycle_grant}}` without `credits` → **reject**.
- `{grant: {mode: allowance, credits: 500}}` → **reject** (extra fields on
  allowance variant).

### Note
The `grant` union is a model-level concern independent of the SQL rename. It can
land in Commit B (Python config hardening) provided the SQL sync still reads
`grant.{mode,credits,bucket,replace_prior}` nested keys (which it does under the
end-state design).
