# 04 — JavaScript Parity (Step 5)

Resolves the JS halves of **C1** (delete the now-unneeded adaptation layer),
**H4** (finish `tier`→`bucket` on the write path), **M1** (drop old-key
aliases), **M2** (prefix-match). The adaptation deletion **must land in the same
green-suite window as the SQL rename** (`02-sql-rename.md`), because both sides
of the sync contract change together.

---

## 4.1 C1 — Delete the JS new→old adaptation layer

### Evidence
`javascript/src/billing/postgres-billing-store.ts:79-135` and
`javascript/src/billing/supabase-billing-store.ts:54-81` build a legacy-shaped
object (`credit_topups`, `plan_key`, `entitlement_mode`, `cycle_grant_credits`,
`cycle_grant_tier`, `cycle_grant_replace_prior`, `provider_refs`, `tier`,
`credits_per_major_unit`) before calling `sync_billing_from_config`.

### Fix
Under the renamed SQL (which now reads the new BillingConfig shape natively):
- `postgres-billing-store.ts:84-135` `syncBillingFromConfig`: replace the
  adapted object with `await this.callRpcVoid("sync_billing_from_config",
  [config])` (pass `config` directly, JSON-serialised as the store already does
  for other RPCs). Delete the `adapted` construction and the explanatory
  comment (`:79-83`).
- `supabase-billing-store.ts:54-81`: same — pass `config` directly.
- Read-side simplifications (drop `?? oldName` fallbacks):
  - `postgres-billing-store.ts:160-161`: `return { ...result, plan: result.plan };`
    (drop `?? result.planKey ?? result.plan`).
  - `postgres-billing-store.ts:327`: `creditsPerUnit: result.creditsPerUnit ?? 1000`
    (drop `?? result.creditsPerMajorUnit`).
  - `supabase-billing-store.ts:117` and `:248`: same fallback drops.
- Subscription upserts (`postgres-billing-store.ts:213-297`,
  `supabase-billing-store.ts`): use `plan` column/field (renamed) instead of
  `plan_key`; emit `plan` not `planKey`.

---

## 4.2 H4 — Finish `tier`→`bucket` on the JS write path

### Evidence
Read path fully renamed (`getBucketBalances`, `bucketKey`,
`BucketBalancesResult`), but the write path still takes `tier?: string`:
`manager.ts:174` (`GrantSubscriptionCycleOptions.tier`), `:474` (`addCredits`
options), `:521` (`deductCredits` options), `:573,603,615,630` (cycle-grant
usage), and the store `addCredits` signature in `credit-store.ts`. Python uses
`bucket` everywhere (`interface/base.py:172`, `manager.py:290,325,367`). Split
API + cross-SDK parity defect.

### Fix
- `javascript/src/stores/credit-store.ts`: rename the `addCredits` abstract
  param `tier` → `bucket` (and any `deduct`/`grant` params).
- `javascript/src/stores/memory-store.ts`, `postgres-store.ts`,
  `supabase-store.ts`: rename the `addCredits`/`deduct`/cycle-grant `tier` param
  → `bucket` to match the abstract signature.
- `javascript/src/manager.ts`:
  - `GrantSubscriptionCycleOptions.tier` (`:174`) → `bucket`.
  - `addCredits` options (`:474`) → `bucket`; pass `options?.bucket` to
    `store.addCredits` (`:486`).
  - `deductCredits` options (`:521`) → `bucket`; pass `options?.bucket` (`:532`).
  - Cycle-grant local `const tier = options?.tier ?? "subscription"` (`:573`)
    → `const bucket = options?.bucket ?? "subscription"`; update all downstream
    uses (`:581,592,603,615,630`), including `bucketsBefore.buckets.find((t) =>
    t.bucketKey === tier)` (`:592`) → `=== bucket`.
- Update tests that pass `{ tier: "…" }` to `addCredits`/`deductCredits`/
  `grantSubscriptionCycle` → `{ bucket: "…" }` (`credit-manager.test.ts`,
  `memory-store.test.ts`, `tiers.test.ts`, `store-integration.test.ts`, etc.).

### Note
The Python `revoke_credits_by_tx_type` doc (`interface/base.py:661`) says
"across tiers" — cosmetic; leave or fix in the comment sweep (`07-cosmetic.md`).

---

## 4.3 M1 — Drop old deprecated key aliases

### Evidence
`config.ts:205-235` `normaliseKeys` maps OLD names as aliases:
`signup_bonus→signupGrant`, `free_allowance→freeAllowance`,
`default_billing_mode→defaultBillingMode`, `feature_limits→entitlements`,
`allowance_period→allowancePeriod`, `default_ttl_days→ttlDays`,
`is_default→isDefaultBucket`, `is_default_bucket→isDefaultBucket`.
`memory-store.ts:66-183` (`normalisePlanDefinition`, `normaliseBucketDefinition`,
`normaliseFeatureLimitsMap`) accepts the same old names as fallbacks. The plan
forbids backward-compat shims; Python (Pydantic `extra="forbid"`) rejects old
keys. Cross-SDK parity bug.

### Fix
- `config.ts:205-235` `keyMap`: keep only **new-key** snake→camel mappings:
  `min_balance→minBalance`, `signup_grant→signupGrant`, `rate_overrides→
  rateOverrides`, `billing_mode→billingMode`, `overdraft_floor→overdraftFloor`,
  `per_operation→perOperation`, `max_concurrent→maxConcurrent`, `ttl_days→
  ttlDays`, `allow_overdraft→allowOverdraft`, `cache_discount→cacheDiscount`,
  `flat_jobs→flatJobs`, `interval_count→intervalCount`, `max_calls→maxCalls`,
  `on_exceed→onExceed`, `min_amount_minor→minAmountMinor`, `max_amount_minor→
  maxAmountMinor`, `tax_behavior→taxBehavior`, `deposit_to→depositTo`,
  `credits_per_unit→creditsPerUnit`, `replace_prior→replacePrior`,
  `product_id→productId`, `price_id→priceId`, `variant_id→variantId`,
  `lookup_key→lookupKey`, plus the identity section keys (`metering`, `ledger`,
  `plans`, `billing`, `allowance`, `safety`, `entitlements`, `label`,
  `providers`, `grant`, `version`).
  **Remove:** `signup_bonus`, `free_allowance`/`freeAllowance`,
  `default_billing_mode`/`defaultBillingMode`, `feature_limits`,
  `allowance_period`/`allowancePeriod`, `default_ttl_days`/`defaultTtlDays`,
  `is_default`, `is_default_bucket`, and any `name`/`action` aliases.
- `memory-store.ts:66-122` `normalisePlanDefinition`: remove the
  `?? p["freeAllowance"] ?? p["free_allowance"]`, `?? p["allowancePeriod"] ??
  p["allowance_period"]`, `?? p["defaultBillingMode"] ?? … ??
  p["default_billing_mode"]`, `?? p["featureLimits"] ?? p["feature_limits"]`,
  `?? p["name"]` fallbacks — read only the new nested shape
  (`allowance.amount`, `allowance.period`, `safety.billingMode`,
  `safety.perOperation`, `safety.maxConcurrent`, `safety.overdraftFloor`,
  `rateOverrides`, `entitlements`, `label`).
- `memory-store.ts:129-161` `normaliseFeatureLimitsMap`: remove the
  `?? l["max_calls"]` and `?? l["action"]` fallbacks (`:150,156`).
- `memory-store.ts:168-183` `normaliseBucketDefinition`: remove the
  `?? t["defaultTtlDays"] ?? t["default_ttl_days"]` and
  `?? t["isDefault"] ?? t["is_default"]` and `?? t["name"]` fallbacks.
- Update the `normaliseKeys` doc comment (`config.ts:198-204`) — it currently
  cites `free_allowance`/`default_billing_mode` as documented keys; rewrite to
  cite new keys (`signup_grant`, `billing_mode`, `ttl_days`, …).
- Add a parity case asserting an old flat config (`models` at top level,
  `_default`, `signup_bonus`, `free_allowance`) is **rejected** by both SDKs
  (`05-tests-parity.md`).

> After this change, configs loaded directly into `MemoryStore` (the raw-dict
> path that bypasses `config.ts` `assertKnownKeys`) no longer silently accept
> old keys, matching Python.

---

## 4.4 M2 — `calcModel` prefix-match

### Evidence
`engine.ts:96-104` `resolveModel` implements exact → prefix (`startsWith`,
skipping `*`) → `*` → `null`, but is **dead code** (never called). The actual
pricing path `calcModel` (`engine.ts:134-150`) does only exact → `*` → throw.
So `claude-sonnet-4-20250514` is priced via `*`, not via a `claude-sonnet-4`
prefix key. The plan explicitly specifies prefix-match resolution. (Verify
Python does prefix-match for parity; if not, this is a shared spec gap to fix in
both.)

### Fix
- `engine.ts:134-150` `calcModel`: resolve the key via `resolveModel` first,
  then evaluate the resolved expression. Concretely:
  ```ts
  const key = this.resolveModel(model);            // exact→prefix→*→null
  if (key === null) throw new ConfigError(`no metering model for ${model}`);
  const expr = this.config.metering.models![key];
  return this.eval(expr, this.buildVariables(metrics));
  ```
- Delete the duplicate exact/`*` logic in `calcModel`; keep `resolveModel` as
  the single resolver.
- `engine.ts:154` default tools expression `"tool_calls * 0"`: align with
  `config.ts:387` `"calls * 0"` (cosmetic; both evaluate to 0).

### Parity check
Confirm `python/src/bursar/engine.py` `resolve_model`/`_calc_model` does
prefix-match. If Python also lacks it, file a shared spec gap and fix both
(`_calc_model`: exact → prefix → `*` → raise).
