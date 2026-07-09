# 06 — Docs, Samples & Notebook (Step 7) [H1]

Phase 6 of the original plan is **entirely undone** —
`git diff origin/main -- docs/ samples/ python/README.md javascript/README.md`
is empty. The docs still describe the old flat schema (`_default`,
`this_tool_calls`, `signup_bonus`, `free_allowance`, `feature_limits`,
`credit_topups`, `tier`), which will mislead integrators. Acceptance criterion 7
("notebook 15 green") cannot be met until this lands. Commit E.

---

## 6.1 Docs (`docs/docs/`)

| File | Required work |
|------|---------------|
| `configuration.mdx` | **Full rewrite.** Four sections (metering / ledger / plans / billing), YAML + JSON examples, `version: 1`, `*` fallback, `calls` (tools-only), `cache_discount` positive-sign, `flat_jobs`, `ledger.buckets` (priority, `ttl_days`, `default`, `allow_overdraft`), `plans.*.{label,allowance,safety,entitlements}`, `billing.{currency,subscriptions,topups}` with `grant` discriminated union and `providers`/`ProviderRef`. Drop all old-key examples. |
| `expressions.mdx` | `calls` (not `this_tool_calls`); `calls` valid only in `metering.tools`; `cache_discount` is a **positive** number the engine subtracts (not the old negative `cache`); global variable list unchanged. |
| `subscription-integration.mdx` | `grant: {mode: allowance}` vs `{mode: cycle_grant, credits, bucket, replace_prior}`; `providers` map (key = provider, value = `ProviderRef`); `deposit_to` (not `tier`); `credits_per_unit` (not `credits_per_major_unit`); global `billing.currency`. |
| `cli.mdx` | `bursar config schema` emits the new JSON Schema; `bursar config set` validates the new shape. Update example output. |
| `python-api/pricing-engine.mdx` | `pricing_schema()` returns the single `PricingConfig`; `get_flat_job_cost` (not `get_fixed_cost`); `UsageMetrics.flat_job` (not `fixed_job`). |
| `javascript-api/index.mdx` | `pricingSchema()`; `getFlatJobCost`; `UsageMetrics.flatJob`; `getBucketBalances` (not `getCreditTiers`); `bucket` param on `addCredits`/`deductCredits`. |
| `python-api/index.mdx` | Update exported symbols: `BucketDefinition`, `BucketBalance`, `BucketBalancesResult`, `ProviderRef`, `AllowanceGrant`, `CycleGrant`. |

---

## 6.2 READMEs + samples

- `python/README.md`, `javascript/README.md`: config snippets in the new shape;
  `python/README.md:313` currently says `engine.pricingSchema(): PricingConfigData`
  → `Record<string, unknown>` (or the new `PricingConfig`).
- Locate and update sample `pricing.{yaml,json}` fixtures under `samples/` and
  any `docs/` example files: convert `models`/`tools`/`search`/`cache`/`fixed`
  top-level → `metering.*`; `min_balance`/`signup_bonus`/`tiers` → `ledger.*`
  (`signup_grant`, `buckets`); `_default` → `*`; `name`→`label`,
  `free_allowance`+`allowance_period`→`allowance.{amount,period}`,
  `default_billing_mode`→`safety.billing_mode`, `features`+`feature_limits`→
  `entitlements`; `subscriptions`/`credit_topups` → `billing.subscriptions`/
  `billing.topups` with `grant`/`providers`/`deposit_to`/`credits_per_unit`.

---

## 6.3 Notebooks

- `samples/python/notebooks/15_pricing_config_schema.ipynb` — **full rewrite**
  (acceptance artifact). Every cell must run against the new schema, including
  the `BillingConfig.from_pricing_config` path replacing the old manual
  `BillingOffer(offer_key=…)` rewrap. Cover: four sections, `*` fallback,
  `calls`, `cache_discount` positive sign, `flat_jobs`, `ledger.buckets`,
  `plans.*.entitlements`, `billing.subscriptions.*.grant` (both modes),
  `billing.topups.*.deposit_to`/`credits_per_unit`.
- `samples/python/notebooks/12_cli_and_deployment.ipynb` — update any cell that
  prints/sets config (`bursar config schema`/`set` output).
- `samples/python/notebooks/13_credit_tiers.ipynb` — the title says "tiers";
  update config snippets to `ledger.buckets` and result types to `Bucket*`.
  (Rename the file itself only if desired; the content must match the new
  schema regardless.)

---

## 6.4 Generated schema doc

- `docs/generated/schema/` and `docs/generated/api/openapi.json` (if regenerated
  from `database.types.ts` / route handlers) — regenerate after the SQL rename
  so the renamed tables/RPCs (`credit_buckets`, `sync_buckets_from_config`,
  `get_user_credit_buckets`, renamed `credit_plans`/`billing_offers`/
  `billing_credit_topups` columns) are reflected. Confirm the generation script
  is re-run as part of this step.
