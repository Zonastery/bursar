# 07 — Cosmetic & Naming Sweep (Step 8) [M6, L1–L4]

Low-risk cleanup, no behavioral impact. Commit F, after all functional commits.
Run the full suite after each cluster.

---

## 7.1 L1 — Rename JS memory-store private helpers

`javascript/src/stores/memory-store.ts:301-324` — public API was renamed
(`getBucketBalances`) but private helpers kept `tier` names though their bodies
operate on `bucketBalances`/`bucketDefinitions`:

- `tierBalanceKey` (`:301`) → `bucketBalanceKey`
- `getTierBalance` (`:305`) → `getBucketBalance`
- `adjustTierBalance` (`:309`) → `adjustBucketBalance`
- `tierWalkOrder` (`:324`) → `bucketWalkOrder`

Update all call sites (`:519,575,579,592,1504,1507,1648,1723,1724,1732,1790`)
and the doc block (`:315-322`: "tier-priority walk", "configured tiers",
"tier keys", `tier_breakdown` → "bucket-priority walk", "configured buckets",
"bucket keys", `bucketBreakdown`).

---

## 7.2 L2 — Rename `fixedCredits` → `flatJobCredits`

The method was renamed (`calcFlatJobs`/`getFlatJobCost`) but the result
variable and public breakdown field still say "fixed":

- `javascript/src/engine.ts:35` (`const fixedCredits = this.calcFlatJobs(metrics)`)
  → `flatJobCredits`; update `:44` (return field).
- `javascript/src/breakdown.ts:17` (field def), `:27,34,50` (usages) →
  `flatJobCredits`.
- Update `CostBreakdown` consumers in tests (`engine.test.ts`,
  `pricing-cache.test.ts`) accordingly.

> Python uses `flat_job_credits` / `flat_jobs` already — verify parity of the
> breakdown field name across SDKs after the rename.

---

## 7.3 M6 — Stale doc comments referencing old names

| File:line | Old reference | Fix |
|-----------|---------------|-----|
| `javascript/src/manager.ts:135` | `getCreditTiers`, `deductFixed` | `getBucketBalances`, `deductFlatJob` |
| `javascript/src/types.ts:229` | `defaultBillingMode` | `billingMode` |
| `javascript/src/allowance.ts:11` | `PlanDefinition.featureLimits` | `entitlements` |
| `javascript/src/expr.ts:158` | `_default` example | `*` example |
| `javascript/src/stores/memory-store.ts:125` | "featureLimits/feature_limits map" | "entitlements map" |
| `javascript/src/stores/memory-store.ts:320` | `tier_breakdown` | `bucketBreakdown` |
| `javascript/src/stores/postgres-store.ts:81` | `tier_key`/`tier_breakdown` | `bucket_key`/`bucketBreakdown` |
| `javascript/src/stores/supabase-store.ts:82` | `tier_key`/`tier_breakdown` | `bucket_key`/`bucketBreakdown` |
| `python/src/bursar/manager.py:169` | `deduct_fixed` | `deduct_flat_job` |
| `python/src/bursar/sql/014_cycle_grant.sql:73,99` | "tiers" / "per-tier invariant" | "buckets" / "per-bucket invariant" |
| `python/src/bursar/interface/base.py:661` | "across tiers" | "across buckets" |

Also update stale test titles/labels:
- `javascript/tests/engine.test.ts:253` — "returns config as BursarConfigData"
  → "returns config as Record<string, unknown>".
- `javascript/tests/memory-store.test.ts:2008-2010`,
  `credit-manager.test.ts:1666,1691` — `getCreditTiers` references in
  comments/titles → `getBucketBalances`.

---

## 7.4 L4 — Delete `BursarConfigData` (JS)

`javascript/src/types.ts:4-5` is a `@deprecated` alias to
`Record<string, unknown>` with **zero internal usages** (the old structured
model was replaced by `BursarConfig` in `config.ts:73`). Delete it. Remove any
external-facing mention in `javascript/README.md:313` (already covered in
`06-docs-samples.md`).

---

## 7.5 L3 — `signup_bonus` transaction type (optional, out of scope)

`memory-store.ts:1699` uses `tx.type === "signup_bonus"`, and the SQL trigger
is `grant_signup_bonus()` (`010_credit_tiers.sql:153`). The config KEY was
renamed to `signup_grant`, but the **transaction-type discriminator** is
consistent across SDK + SQL and is a persisted value. **Leave as-is.** Renaming
would require coordinated changes across the JS SDK, tests, and the SQL
trigger, with no functional benefit. Noted only for completeness.
