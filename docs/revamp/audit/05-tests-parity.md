# 05 — Tests & Parity Fixture (Step 6)

Update the shared parity fixture and add the real-DB billing-sync test that
would have caught C1. Land as Commit D (after the SQL-rename + JS commits).

---

## 5.1 Shared parity fixture — `tests/parity/config_validation_cases.json`

Add cases (each must agree accept/reject across `python/tests/test_config_parity.py`
and `javascript/tests/config-parity.test.ts`):

| Case | Config excerpt | Expect | Covers |
|------|----------------|--------|--------|
| `old_flat_config_rejected` | top-level `models`/`signup_bonus`/`free_allowance` (no `metering`/`ledger`) | reject | M1 — no compat shim |
| `old_wildcard_default_rejected` | `models: {_default: "input_tokens*1"}` | reject | M1 — `_default` gone |
| `old_tool_var_rejected` | `metering.tools: {"x": "this_tool_calls*5"}` | reject | M1 — `this_tool_calls` gone |
| `grant_cycle_missing_credits_rejected` | `billing.subscriptions.a.grant: {mode: cycle_grant}` | reject | C3 |
| `grant_allowance_extra_fields_rejected` | `billing.subscriptions.a.grant: {mode: allowance, credits: 500}` | reject | C3 |
| `subscription_plan_dangling_no_plans_rejected` | `billing.subscriptions.a.plan: "pro"` with no `plans` | reject | H3 |
| `subscription_plan_dangling_unknown_rejected` | `plan: "nope"` with `plans: {pro: …}` | reject | H3 (already covered?) |
| `models_prefix_match_accepted` | `models: {"claude-sonnet-4": "input_tokens*3", "*": "input_tokens*10"}` + metric `claude-sonnet-4-20250514` → resolves to prefix key | accept (value) | M2 (engine parity, if fixture supports value checks) |
| `topups_deposit_to_accepted` | `billing.topups.x.deposit_to: "purchased"` | accept | full rename |
| `topups_old_tier_rejected` | `billing.topups.x.tier: "purchased"` | reject | M1 |

Also audit existing cases that still reference old names in their `name`/`reason`
strings (e.g. `negative_signup_bonus_rejected` `:56`, `empty_tiers_dict_rejected`
`:76`, `plan_missing_name_rejected` `:141`, `plan_negative_free_allowance_rejected`
`:160`) — rename the case `name` fields to the new vocabulary
(`signup_grant`, `buckets`, `label`, `allowance`) and update the configs to the
new shape while preserving intent.

---

## 5.2 New Python real-DB billing-sync test  [C1 regression guard]

`python/tests/test_billing_integration.py` (or `test_store_integration.py`)
currently uses `MemoryBillingStore` only (`:124`). Add a real-Postgres case:

```python
def test_sync_billing_from_config_postgres(real_pg_store):
    bs = PostgresBillingStore(real_pg_store.pool)
    bs.sync_billing_from_config(_BILLING_CONFIG)          # must not raise
    offer = bs.resolve_billing_offer("stripe", price_id="price_monthly_1000")
    assert offer and offer["plan"] == "pro"               # renamed column
    topup = bs.resolve_credit_topup("stripe", price_id="price_topup_credits")
    assert topup and topup["deposit_to"] == "purchased"   # renamed column
    assert topup["credits_per_unit"] == 1000              # renamed column
```

- Assert subscriptions land in `billing_offers` with `plan`/`grant_mode`/
  `grant_credits`/`grant_bucket`/`grant_replace_prior` (renamed).
- Assert topups land in `billing_credit_topups` with `deposit_to`/
  `credits_per_unit` and **no** `currency`/`tier`/`credits_per_major_unit`
  columns.
- Add an equivalent `SupabaseStore`/`HttpxSupabaseStore` case if the suite has
  a real-Supabase harness; otherwise rely on the Postgres path + the
  Supabase-store unit tests against a mocked client.
- Add a `setup_pricing` round-trip case: publish a pricing config with a
  `billing` section and assert offers/topups sync via the
  `setup_pricing → sync_billing_from_config(p_config->'billing')` path (C2).

---

## 5.3 Existing test updates

Re-scan the updated test files for renamed-symbol correctness after Steps 1-4:

- `python/tests/test_billing_integration.py:693` — `offer["offer_key"]` stays
  (PK); add `offer["plan"]` assertion. `BillingSubscriptionState` uses `plan`
  not `plan_key`.
- `python/tests/test_billing_manager.py` — `BillingOffer(plan=…)` already new;
  update any `grant=SubscriptionGrant(mode=…)` to the union variants
  (`AllowanceGrant()` / `CycleGrant(mode="cycle_grant", credits=…)`).
- `python/tests/test_subscription_cycle.py`, `test_billing_integration.py` —
  cycle-grant bucket param renamed in Python (`bucket`); confirm JS tests use
  `bucket` after 4.2.
- JS `billing-integration.test.ts:326,1002` — `syncBillingFromConfig({...})`
  raw objects: ensure they use the new shape (`plan`, `grant`, `providers`,
  `deposit_to`, `credits_per_unit`), not old keys.
- `javascript/tests/engine.test.ts:253` — test title "returns config as
  PricingConfigData" → "returns config as Record<string, unknown>" (cosmetic,
  but do it in this pass).

---

## 5.4 Parity runner sanity

`python/tests/test_config_parity.py` and `javascript/tests/config-parity.test.ts`
load `config_validation_cases.json` directly and require accept/reject
agreement. After 5.1, run both and confirm green. These runners themselves need
no structural change (only the fixture changes), per the plan.
