# 02 — Full SQL Rename (Step 1) + Store Fallback Cleanup (Step 3)

Resolves **H2**, and is the SQL half of **C1**/**C2**. Buckets were already
fully renamed (010: `credit_buckets`, `user_credit_buckets`,
`sync_buckets_from_config`, `get_user_credit_buckets`, metadata `bucket`/
`bucket_breakdown`). This step finishes the same treatment for plans and
billing.

Unreleased module, fresh-DB only: rewrite the numbered migrations in place
(`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE FUNCTION`). No
`ALTER TABLE … RENAME`, no data migration.

---

## 2.1 `python/src/bursar/sql/004_plans.sql` — plans

### `credit_plans` table (`004:7-23`)
Rename columns:
- `name` → `label`
- `free_allowance` → `allowance_amount`
- `default_billing_mode` → `billing_mode`
- `features` → `entitlements`
- **DROP** `feature_limits` (`004:14`) — dead, always written `'{}'::jsonb`
  (`004:105`).

Keep unchanged: `plan_key` (stores the config map key), `allowance_period`,
`rate_overrides`, `per_operation`, `max_concurrent`, `overdraft_floor`,
`description`, `id`, timestamps.

Also:
- Delete the `ALTER TABLE … ADD COLUMN IF NOT EXISTS feature_limits` block
  (`004:31-34`) and its comment — the column no longer exists.

### `sync_plans_from_config` (`004:81-128`)
- INSERT/UPDATE column list: use renamed columns (`label`, `allowance_amount`,
  `billing_mode`, `entitlements`); drop `feature_limits` from the list and stop
  writing `'{}'::jsonb` (`004:105`).
- The function already reads the new config shape (`label`, `allowance.amount`,
  `safety.billing_mode`, `safety.per_operation`, `safety.max_concurrent`,
  `safety.overdraft_floor`, `allowance.period`, `entitlements`) — no read-side
  change needed.

### `get_user_plan` (`004:133-180`)
- SELECT renamed columns (`label`, `allowance_amount`, `billing_mode`,
  `entitlements`); drop `feature_limits` from the SELECT and variable list.
- Output JSON keys already new (`plan_label`, `allowance_amount`,
  `entitlements`, `billing_mode`) at `004:166-178` — keep.

### `set_user_plan` / `check_plan_allowance` / `increment_usage_window`
- `set_user_plan(p_plan_key)` (`004:201-237`): param `p_plan_key` and
  `WHERE plan_key = p_plan_key` (`004:220`) — `plan_key` column stays, so no
  change. (Optionally rename param to `p_plan_id` for clarity; not required.)
- `check_plan_allowance` (`004:290-355`): reads `free_allowance`/
  `allowance_period` (`004:311`) → rename to `allowance_amount`/
  `allowance_period` (latter unchanged).
- `increment_usage_window` (`004:378-417`): no plan-column reads — no change.

---

## 2.2 `python/src/bursar/sql/013_billing.sql` — billing

### `billing_offers` table (`013:9-20`)
Rename columns:
- `plan_key` → `plan`
- `entitlement_mode` → `grant_mode`
- `cycle_grant_credits` → `grant_credits`
- `cycle_grant_tier` → `grant_bucket`
- `cycle_grant_replace_prior` → `grant_replace_prior`

Keep: `offer_key` (PK = config map key), `interval`, `interval_count`,
timestamps.
Rename index `idx_billing_offers_plan_key` (`013:22`) →
`idx_billing_offers_plan`.

### `billing_subscriptions` table (`013:141-148`)
Rename `plan_key` → `plan` (`013:148`). Keep `offer_key` (FK to
`billing_offers.offer_key`).

### `billing_credit_topups` table (`013:431-435`)
Rename columns:
- `tier` → `deposit_to` (`013:433`)
- `credits_per_major_unit` → `credits_per_unit` (`013:435`)
- **DROP** `currency` (`013:434`) — moved to global `billing.currency`
  (`BillingConfig.currency`).

Keep: `topup_key` (PK), `min_amount_minor`, `max_amount_minor`, `tax_behavior`,
timestamps.

### `sync_billing_from_config` (`013:~535-660`)
Change to expect a **BillingConfig-shaped** argument (top-level
`currency`/`subscriptions`/`topups`, new nested field names):
- Guard: `IF p_billing ? 'subscriptions' …` (was `p_config`).
- Per offer (`013:559-612`):
  - `v_item->>'plan'` → `plan` column (was `v_item->>'plan_key'`).
  - `v_item#>>'{grant,mode}'` → `grant_mode` (was `entitlement_mode`).
  - `v_item#>>'{grant,credits}'` → `grant_credits` (was `cycle_grant_credits`).
  - `v_item#>>'{grant,bucket}'` → `grant_bucket` (was `cycle_grant_tier`).
  - `v_item#>>'{grant,replace_prior}'` → `grant_replace_prior`
    (was `cycle_grant_replace_prior`).
  - `v_item->'providers'` → provider refs (was `v_item->'provider_refs'`).
- Per topup (`013:621-660`):
  - Guard `IF p_billing ? 'topups' …` (was `credit_topups`).
  - `v_item->>'deposit_to'` → `deposit_to` column (was `tier`).
  - `v_item->>'credits_per_unit'` → `credits_per_unit` (was
    `credits_per_major_unit`).
  - Drop the `currency` write (`013:630`) — column removed.
  - `v_item->'providers'` (was `provider_refs`).
- INSERT/UPDATE column lists: renamed columns; drop `currency`.

### `resolve_billing_offer_by_price` (`013:~720-734`)
Emit renamed keys: `plan`, `grant_mode`, `grant_credits`, `grant_bucket`,
`grant_replace_prior` (was `plan_key`/`entitlement_mode`/`cycle_grant_*`).
Keep `offer_key`.

### `resolve_credit_topup` (`013:~780-800`)
Emit `deposit_to`, `credits_per_unit` (was `tier`, `credits_per_major_unit`);
drop `currency`. Keep `topup_key`.

### Subscription upsert/get RPCs (`013:~880-960, 1171-1172`)
Use `plan` column/param instead of `plan_key`; emit `plan` in JSON output.
Rename the `p_plan_key` param of `set_user_plan` (`013:487`) only if desired
(not required — the column it compares against stays `plan_key` on
`credit_plans`; this `plan_key` is on `billing_subscriptions`).

> Careful: `billing_subscriptions.plan` and `credit_plans.plan_key` are
> different columns. `billing_subscriptions.plan` stores the subscription's
> plan id; `credit_plans.plan_key` stores the plan's config map key. Rename
> only the `billing_subscriptions` one.

### `setup_pricing` (`013:1216-1222`)
- `PERFORM public.sync_billing_from_config(p_config->'billing');` (was
  `p_config`) — **fixes C2**.
- Remove the `BEGIN … EXCEPTION WHEN OTHERS THEN RAISE WARNING … END` wrapper
  (`013:1218-1222`) once sync is correct, so failures surface.

### `013` lower tables (`billing_payments`, `billing_invoices`,
`billing_refunds`, `billing_disputes`)
These have their own `currency` columns (`013:259,312,377`) for actual money
movements — **keep** them. Only the `billing_credit_topups.currency` column
(topup *config*) is dropped.

---

## 2.3 `python/src/bursar/sql/014_cycle_grant.sql`

No `cycle_grant_*`/`plan_key`/`entitlement_mode` column reads found — the cycle
grant RPC consumes already-resolved grant values from the manager, and the SQL
references to "tiers" (`014:73,99`) are comments about the bucket walk.
**Change:** comment touch-ups only (`per-tier` → `per-bucket`, `walk_tiers` →
`walk_buckets`). Verify by grep after edit.

---

## 2.4 Step 3 — Python interface-store fallback cleanup [H2 cleanup]

`sync_plans_from_config` now writes renamed columns and `get_user_plan` returns
only new keys, so the dual-name fallbacks in the stores are dead.

### `python/src/bursar/interface/postgres.py:627-636`
Drop the `or` fallbacks:
- `plan_label = result_dict.get("plan_label")` (drop `or result_dict.get("plan_name")`).
- `allowance_amount = _dec(result_dict["allowance_amount"])` (drop
  `else _dec(result_dict.get("free_allowance", 0))`).
- `entitlements = {… (result_dict.get("entitlements") or {}).items()}` (drop
  `or result_dict.get("feature_limits")`).
- `billing_mode = str(result_dict.get("billing_mode") or "strict")` (drop
  `or result_dict.get("default_billing_mode")`).

### `python/src/bursar/interface/supabase.py:647-656`
Same fallback removals (mirror of the above).

### Python billing stores (`billing/postgres.py`, `billing/supabase.py`)
- `resolve_billing_offer` result (`postgres.py:91`, `supabase.py:182-183`): read
  `plan`/`grant_mode`/`grant_credits`/`grant_bucket`/`grant_replace_prior`
  (renamed) instead of `plan_key`/`entitlement_mode`/`cycle_grant_*`.
- Subscription upsert/get (`postgres.py:157-293`, `supabase.py` subscription
  methods): use `plan` column/field instead of `plan_key`.
- Topup resolve: read `deposit_to`/`credits_per_unit` (renamed) instead of
  `tier`/`credits_per_major_unit`.

### `python/src/bursar/billing/models.py`
- `BillingSubscriptionState` (`222-235`): rename `plan_key` → `plan` (`228`).
  Keep `offer_key`.
- `BillingConfig.from_bursar_config` (`192-215`): no change (already builds the
  new model); confirm `grant` is the new union (C3).

---

## Risk

This is the money-adjacent, riskiest commit. Land Steps 2.1–2.4 + the Python
billing model/store changes from `01-critical-fixes.md` (C1/C2/C3) as **one
commit**, with the invariant/property tests and the new real-DB billing-sync
test (`05-tests-parity.md`) as the guard. The JS adaptation deletion
(`04-javascript-parity.md`) must land in the same green-suite window.
