# Bursar Config Schema — Revamp Design

Status: proposed (unreleased module — no backward-compatibility constraints).
Scope decisions locked with maintainer:

- Full propagation of every rename through runtime + SQL + public API (not config-only translation).
- Full merge of `features` and `feature_limits` into a single `entitlements` map.
- `*` replaces `_default` as the fallback key for `models`/`tools`.
- **Keep `version: 1`** — we do not bump the schema version integer. The module is
  unreleased, so the entire config shape and codebase can be redesigned in place
  under the same version number. There is no migration path from the old flat
  layout; old configs are simply invalid.

This document defines the target schema. The step-by-step build is in
[implementation-plan.md](implementation-plan.md).

---

## 1. Why the current layout needed a redesign

The config grew feature-by-feature into 12 flat top-level keys that actually
belong to four different domains, with no structural signal of what relates to
what. Concrete problems found in the current code:

- Two competing config models that silently drift: `PricingConfig`
  (`python/src/bursar/config.py`) and `PricingConfigData`
  (`python/src/bursar/interface/models.py`), kept in sync only by a field-set
  parity test. `PricingEngine.pricing_schema()` rebuilds one from the other and
  drops `tiers`/`subscriptions`/`credit_topups`/`signup_bonus`/`version`.
- `subscriptions` / `credit_topups` typed as `dict[str, dict]` — untyped holes
  validated by hand, forcing callers to re-wrap with `BillingOffer(offer_key=k, **v)`.
- Identifiers duplicated as both dict key and inner field: plan `id`, offer
  `offer_key`, and `provider` inside `provider_refs` values.
- Two near-identical provider-ref models (`BillingProviderRefs` and
  `BillingSubscriptionOfferRef`), the latter reused for topups where the name lies.
- Conditionally-meaningful flat fields: `cycle_grant_credits/tier/replace_prior`
  only matter when `entitlement_mode == "cycle_grant"`.
- `cache` is a cost that must evaluate to a negative number — a silent sign footgun.
- Opaque names: `fixed`, `credits_per_major_unit`, `min_amount_minor`.
- `min_balance` (top level) vs `overdraft_floor` (plan/op) — same "how low can
  the balance go" axis, three places, different names.
- `features` vs `feature_limits` — two dicts keyed by feature name; the docs'
  own example puts a roadmap cap in both.
- Legacy `billing_mode` alias for `default_billing_mode` (`_normalise_billing_mode_alias`).
- Tri-state `tiers` (`None` ok, `{}` hard error, non-empty ok).
- JS never parsed `subscriptions`/`credit_topups` from the pricing config at all
  (`javascript/src/config.ts` `TOP_LEVEL_KEYS`), so billing config lived in a
  separate object — a cross-SDK inconsistency.

## 2. Design principles

1. Group by the four domains a reader actually reasons about: metering, ledger,
   plans, billing.
2. One identifier per thing: the dict key is the id everywhere; no inner `id`,
   `offer_key`, or `provider` duplication.
3. Make impossible states unrepresentable: discriminated `grant` union instead
   of conditionally-live flat fields.
4. No hidden sign conventions: discounts are positive numbers; the engine
   applies the sign.
5. One model per concept, fully typed — delete the duplicate config model; type
   the billing sections.
6. Names a first-time integrator understands without reading source.
7. **Same `version: 1` integer** — the version field stays `Literal[1]` in code;
   only the document shape changes. No dual-version support, no compat shim.

## 3. Target schema

```yaml
version: 1   # still the only valid version — shape is new, integer is unchanged

# ── A. METERING — how usage becomes a credit cost ──────────────────────
metering:
  models:                         # required, >=1 entry; "*" is the fallback
    "*": input_tokens * 10 + output_tokens * 30
    claude-sonnet-4-6: input_tokens * 3 + output_tokens * 10
  tools:                          # optional; "*" fallback; `calls` = this tool's count
    "*": calls * 0
    code_exec: calls * 50
  search: search_queries * 10 + search_results * 1   # single expression or omit
  cache_discount: cache_read_tokens * 0.5            # POSITIVE = credits refunded
  flat_jobs:                      # was `fixed`; flat Decimal per job name
    roadmap_gen: 5000

# ── B. LEDGER — how credits are stored and consumed ────────────────────
ledger:
  min_balance: 0                  # balance floor (>= 0)
  signup_grant: 50                # was signup_bonus (>= 0)
  buckets:                        # was `tiers`; consumed low priority number first
    gifted:    { priority: 10, ttl_days: 7 }
    monthly:   { priority: 20, ttl_days: 30 }
    purchased: { priority: 30, default: true, allow_overdraft: true }

# ── C. PLANS — what a customer tier gets ───────────────────────────────
plans:
  seeker:                         # key IS the plan id
    label: Seeker                 # was `name`
    allowance: { amount: 5000, period: calendar_month }
    rate_overrides:
      claude-sonnet-4-6: min(input_tokens * 3 + output_tokens * 9, 50)
    safety:                       # groups the financial-safety knobs
      billing_mode: strict        # was default_billing_mode; no alias
      max_concurrent: 1
      overdraft_floor: 0
      per_operation:
        roadmap_gen: { billing_mode: strict, max_concurrent: 1 }
    entitlements:                 # merges features + feature_limits
      roadmap_gen: { value: true, max_calls: 1, period: daily, on_exceed: deny }
      chat: { value: true }

# ── D. BILLING — how real money becomes credits/plans ──────────────────
billing:
  currency: USD                   # single source of truth for money
  subscriptions:
    seeker-monthly:               # key IS the offer id
      plan: seeker                # was plan_key -> references a plans key
      interval: month
      interval_count: 1
      grant: { mode: allowance }  # discriminated union (see below)
      providers:                  # key IS the provider; value is a ProviderRef
        stripe: { price_id: price_monthly_seeker }
    sage-annual:
      plan: sage
      interval: year
      grant:
        mode: cycle_grant
        credits: 50000
        bucket: purchased         # was cycle_grant_tier
        replace_prior: true       # was cycle_grant_replace_prior
      providers:
        stripe: { price_id: price_annual_sage }
  topups:
    small-pack:
      credits_per_unit: 1000      # was credits_per_major_unit (per 1 major unit of billing.currency)
      min_amount_minor: 500
      max_amount_minor: 50000
      tax_behavior: exclude_tax
      deposit_to: purchased       # was `tier`
      providers:
        stripe: { price_id: price_credits_small }
```

## 4. Section reference

### 4.1 `metering` (required)

- `models` (required, non-empty map): model id -> expression. `"*"` is the
  fallback when no id matches; resolution also allows prefix match
  (`claude-sonnet-4-20250514` -> `claude-sonnet-4`) then `"*"`, else error.
- `tools` (optional map): tool name -> expression. `"*"` covers unlisted tools.
  Tool expressions may use `calls` (count of calls matching this key; for `"*"`,
  the count of unmatched calls) in addition to the global variables. Omitted
  `tools` defaults to `{ "*": "calls * 0" }`.
- `search` (optional single expression): omit for zero.
- `cache_discount` (optional single expression): evaluates to a NON-NEGATIVE
  number of credits refunded; the engine subtracts it. The final total is
  clamped to `>= 0`. (The old flat `cache` key required a negative expression —
  this inverts and documents the sign.)
- `flat_jobs` (optional map): job name -> non-negative Decimal, charged flat and
  bypassing token math. Looked up by `UsageMetrics.flat_job`.

Global expression variables (unchanged set): `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`, `tool_calls`, `search_queries`,
`search_results`, `web_search_calls`, `code_exec_calls`. `calls` is valid ONLY
inside `metering.tools` expressions.

### 4.2 `ledger` (optional)

- `min_balance` (Decimal, default 0, `>= 0`): balance floor.
- `signup_grant` (int, default 50, `>= 0`): credits granted to new users.
- `buckets` (optional map): priority-ordered credit buckets. Omit the key
  entirely for "no buckets" (single synthetic `default` bucket). An explicit
  empty map is rejected. Per bucket:
  - `priority` (int, required): lower number drained first; ties broken by key.
  - `ttl_days` (int > 0, optional): default expiry window when a grant omits an
    explicit expiry. (was `default_ttl_days`.)
  - `expires` (bool, default derived: true when `ttl_days` set, else false).
  - `default` (bool, default false): destination for untagged grants. At most one.
  - `allow_overdraft` (bool, default false): the sole bucket that absorbs
    overdraft debt. At most one.

### 4.3 `plans` (optional)

Map of plan id (the key) -> plan. Fields:

- `label` (string, required): human-readable name. (was `name`.)
- `allowance` (object, optional): `{ amount: Decimal >= 0, period: calendar_month | rolling_30d | anniversary }`. Default amount 0, period `calendar_month`.
- `rate_overrides` (map, optional): model id -> expression, overrides `metering.models` for this plan.
- `safety` (object, optional):
  - `billing_mode` (`strict` | `overdraft`, default `strict`).
  - `max_concurrent` (int | null): cap on simultaneous billed operations.
  - `overdraft_floor` (Decimal | null): negative floor in overdraft mode.
  - `per_operation` (map, optional): operation type -> `{ billing_mode, max_concurrent, overdraft_floor }` override.
- `entitlements` (map, optional): feature name -> entitlement:
  - `value` (any, optional): entitlement value; presence (non-null/non-false) = entitled.
  - `max_calls` (int >= 0, optional): invocation-count limit within `period`.
  - `period` (`daily` | `weekly` | `monthly` | `yearly`, default `monthly`).
  - `on_exceed` (`deny` | `warn` | `notify`, default `deny`). (was `action`.)
  A feature may carry only `value`, only a limit, or both.

### 4.4 `billing` (optional)

- `currency` (string, default `USD`): single currency for all offers/topups.
- `subscriptions` (map): offer id (key) -> offer:
  - `plan` (string, required): references a `plans` key. (was `plan_key`.)
  - `interval` (`day`|`week`|`month`|`year`, default `month`), `interval_count` (int >= 1, default 1).
  - `grant` (discriminated union on `mode`):
    - `{ mode: allowance }` — subscription resets the plan's `allowance`.
    - `{ mode: cycle_grant, credits: int, bucket: string = purchased, replace_prior: bool = true }` — deposits fixed credits each cycle.
  - `providers` (map): provider name (key) -> `ProviderRef`.
- `topups` (map): topup id (key) -> topup:
  - `credits_per_unit` (int, default 1000): credits per 1 major currency unit.
  - `min_amount_minor` / `max_amount_minor` (int).
  - `tax_behavior` (`exclude_tax` | `include_tax`, default `exclude_tax`).
  - `deposit_to` (string, default `purchased`): destination bucket. (was `tier`.)
  - `providers` (map): provider name (key) -> `ProviderRef`.

`ProviderRef` (single unified type; replaces both `BillingProviderRefs` and
`BillingSubscriptionOfferRef`): `{ product_id?, price_id?, variant_id?, lookup_key? }`.
The provider name is the map key — never repeated inside the value.

## 5. Full rename map (old -> new)

Propagated through config, runtime models, SQL, and public API in both SDKs.

Top-level structure:
- `models`, `tools`, `search`, `cache`, `fixed` -> under `metering`.
- `min_balance`, `signup_bonus`, `tiers` -> under `ledger`.
- `subscriptions`, `credit_topups` -> under `billing.subscriptions` / `billing.topups`.

Metering:
- `fixed` -> `metering.flat_jobs`; `UsageMetrics.fixed_job` -> `flat_job`; `get_fixed_cost()` -> `get_flat_job_cost()`.
- `cache` (negative) -> `metering.cache_discount` (positive); engine negates.
- `this_tool_calls` -> `calls`.
- `_default` -> `*` (models + tools).

Ledger / buckets:
- `signup_bonus` -> `ledger.signup_grant`; `min_balance` -> `ledger.min_balance`.
- `tiers` -> `ledger.buckets`; `default_ttl_days` -> `ttl_days`.
- `TierDefinition` -> `BucketDefinition`; `TierBalance` -> `BucketBalance`; `TierBalancesResult.tiers` -> `.buckets`, `tier_key` -> `bucket_key`.
- `get_tier_balances` -> `get_bucket_balances` (+ JS `getTierBalances` -> `getBucketBalances`).
- SQL: `credit_tiers` -> `credit_buckets`, `user_credit_tiers` -> `user_credit_buckets`, `sync_tiers_from_config` -> `sync_buckets_from_config`, `get_user_credit_tiers` -> `get_user_credit_buckets`.
- Tx metadata keys: `tier` -> `bucket`, `tier_breakdown` -> `bucket_breakdown`.
- Result fields: `DeductionResult.tier_breakdown` / `RefundResult.tier_breakdown` -> `bucket_breakdown`; `SweepResult.expired_by_tier` -> `expired_by_bucket`.

Plans:
- `name` -> `label`; drop `id` (key is id).
- `free_allowance` + `allowance_period` -> `allowance.{amount, period}`.
- `default_billing_mode` (+ `billing_mode` alias) -> `safety.billing_mode`.
- `max_concurrent` -> `safety.max_concurrent`; `overdraft_floor` -> `safety.overdraft_floor`; `per_operation` -> `safety.per_operation`.
- `features` + `feature_limits` -> `entitlements` (merged); `FeatureLimit.action` -> `on_exceed`.

Billing:
- `BillingOffer.offer_key` -> removed (injected from map key).
- `plan_key` -> `plan`.
- `entitlement_mode` + `cycle_grant_credits` + `cycle_grant_tier` + `cycle_grant_replace_prior` -> `grant.{mode, credits, bucket, replace_prior}`.
- `BillingProviderRefs` + `BillingSubscriptionOfferRef` -> `ProviderRef`.
- `BillingCreditTopup.credits_per_major_unit` -> `credits_per_unit`; `.tier` -> `deposit_to`; per-topup `currency` -> global `billing.currency`.

Models:
- Delete `PricingConfigData`; single `PricingConfig` persisted and returned by `PricingEngine.pricing_schema()`.

## 6. Validation rules

Enforced at load in both SDKs (shared fixture `tests/parity/config_validation_cases.json`):

1. `version`, if present, must be `1` (int) — unchanged from today; only the document shape is new.
2. `metering.models` present and non-empty.
3. All expressions parse and are safe; only allowed functions; no `**`.
4. `calls` only inside `metering.tools`; global vars elsewhere; unknown variable rejected.
5. Unknown keys rejected at every level (`extra="forbid"`).
6. `ledger.min_balance`, `ledger.signup_grant`, `metering.flat_jobs.*`, `plans.*.allowance.amount` all `>= 0`.
7. `ledger.buckets`: not an empty map; at most one `default`; at most one `allow_overdraft`; `ttl_days > 0` when set.
8. Plans: every plan has `label`; plan labels unique.
9. `plans.*.entitlements.*`: `max_calls >= 0`; valid `period`/`on_exceed`.
10. `plans.*.safety.billing_mode` and `per_operation.*.billing_mode` in {`strict`, `overdraft`}.
11. `billing.subscriptions.*.plan` references an existing `plans` key.
12. `billing.subscriptions.*.grant` valid per discriminated `mode`.
13. `cache_discount` may be any valid expression; documented as non-negative (engine subtracts it).

## 7. What we are NOT doing

- **No `version: 2`** — the integer stays `1`; we replace the old flat layout wholesale.
- **No backward-compat loader** — old flat configs (`models:` at top level, `_default`, etc.) are rejected at validation; there is no translation shim.
- **No data migration** — unreleased module; dev/test databases are recreated on a clean `setup()`.
