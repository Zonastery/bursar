# Authoring the BursarConfig document

`BursarConfig` is one strict, versioned configuration document that drives
every pricing, credit, plan, and commerce decision in Bursar. Pydantic defines
the Python contract and generates the published JSON Schema; AJV validates the
same contract in JavaScript — the same document bills identically in both SDKs.

## Top-level structure

Only `version` and `credits` are required. Every section key comes from
`pricing-config.schema.json` (the definitive reference — print it with
`bursar config schema`):

| Section        | Purpose                                                                                                        |
| -------------- | -------------------------------------------------------------------------------------------------------------- |
| `version`      | Always `1` (the schema constrains it).                                                                         |
| `catalog`      | Catalog-wide settings; `default_plan` names the signup plan (falls back to the lowest-rank plan when omitted). |
| `pricing`      | `operations` (measures + dimensions) and `rate_cards`.                                                         |
| `credits`      | Required. `accounting`, `buckets`, `default_bucket`, `policies`, `grant_programs`, `display`.                  |
| `entitlements` | `features` — typed feature definitions referenced by plan feature values.                                      |
| `admission`    | `policies` — reusable concurrency limits.                                                                      |
| `plans`        | Product plans keyed by stable snake_case identifiers.                                                          |
| `commerce`     | `providers`, `offers`, `subscription_changes`, `auto_recharge`.                                                |

`credits.accounting` is a fixed v1 convention (`unit: credit`, `scale: 6`,
`rounding: half_up`) that may be omitted — canonical output materializes it.
The schema is strict (`additionalProperties: false` everywhere): unknown
fields and legacy shapes are rejected with a `ConfigError`; the same document
loads everywhere via the SDKs' config loaders.

## Operations, measures, and rate cards

Pricing has four layers: an **operation** declares inputs, a **rate card**
names a pricing table, a **price rule** selects a charge, and the
`PricingEngine` evaluates the charge in exact decimal arithmetic.

An operation declares **measures** (numeric quantities with a unit, e.g.
`input_tokens: { unit: token }`) and **dimensions** (typed, non-priced
selectors, e.g. `model: { type: string }`; may set `required: false`).

A rate card is keyed by operation; cards inherit unpriced operations from a
parent via `extends`. Each plan references exactly one card (`plan.rate_card`).
Rules are ordered — the **first matching rule wins**; `when` matchers and
dimension types must agree:

| Operator        | Dimension type | Match                              |
| --------------- | -------------- | ---------------------------------- |
| `eq`            | any            | Exact equality                     |
| `in` / `not_in` | any            | Membership / non-membership        |
| `prefix`        | string         | String prefix                      |
| `range`         | number         | Bounds `gt` / `gte` / `lt` / `lte` |

Every operation on a card needs an `unmatched` policy: `action: reject` refuses
the event, `action: charge` applies a fallback charge. The canonical `standard`
card prices gpt-4o/gpt-4o-mini via a `sum` of `per_unit` charges (`rate` per
`unit_size` units; `unit_size: "1000000"` = per-million-token rates), falls
back to an `expression`, and `reject`s unmetered models.

### Charge types

| Type         | What it does                                                                                |
| ------------ | ------------------------------------------------------------------------------------------- |
| `flat`       | Constant fee per event (`amount`)                                                           |
| `per_unit`   | Rate per `unit_size` units — the per-million-token pricing basis                            |
| `package`    | Price per fixed block of `units`, `rounding: ceil` \| `floor` \| `nearest` (default `ceil`) |
| `graduated`  | Marginal rates per tier (strictly increasing; one open tier at the end)                     |
| `volume`     | One rate selected by total volume                                                           |
| `expression` | Arbitrary formula over the operation's measures                                             |
| `sum`        | Sum of sub-charges (`components`, each any charge type)                                     |

## Price expressions

`expression` charges use a safe allowlisted language, byte-identical in Python
and JavaScript, evaluated in exact `Decimal` arithmetic (never binary float).
`**`/exponentiation is **rejected at config load** (DoS hardening); division or
modulo by zero raises `ExpressionError`; chained comparisons (`a < b < c`) are
rejected so both engines agree; every identifier must be a known measure or
function and at least one measure must be referenced — unknown names fail
validation at load time.

- Arithmetic: `+ - * / // %`; comparisons: `== != < <= > >=`, `in` / `not in`
  (substring); boolean: `and or not`; ternary `x if cond else y`
- Functions: `ceil`, `floor`, `round(x[, n])` (half-up), `min`, `max`,
  `if(c,t,e)`, `tier(value, t1, r1, [t2, r2, ...], default)`, `clamp(x, lo,
hi)`, `percentile(p, v1, ...)` (`0 ≤ p ≤ 100`)

Examples from the sources:

```yaml
charge: { type: expression, formula: input_tokens * 0.005 + output_tokens * 0.015 }
# tiering: small batch 0.01, mid 0.008, big 0.006 per output token
charge: { type: expression, formula: "tier(output_tokens, 1000, 0.01, 10000, 0.008, 0.006)" }
# floor: never below 50 credits
charge: { type: expression, formula: "max(50, input_tokens * 2)" }
```

`tier()` returns the rate of the first threshold where `value < t_i`, else the
trailing `default`; `percentile(50, ...)` is the median, `0/100` the min/max.
Dimensions are not expression variables — they select rules only.

## Plans

A plan bundles what a tier gets. Fields (from `PlanDefinition`):
`display_name` (required), `rank` (default 0; lower first), `description`,
`rate_card`, `allowed_operations`, `features` (values must match the
entitlement definitions), `credit_allowance` (`amount`, optional `priority`,
`window`), `quotas`, `credit_policy` (`prepaid` — floor at zero — or
`credit_line` with `limit`), `admission_policy`, `evolution`.

```yaml
plans:
  free:
    display_name: Free
    rate_card: standard
    allowed_operations: [completion]
    credit_allowance:
      amount: "10000"
      priority: 5
      window: { type: calendar, unit: month, count: 1 }
  pro:
    display_name: Pro
    rate_card: standard
    allowed_operations: [completion, execution]
    quotas:
      daily_tokens:
        operation: completion
        measure: output_tokens
        limit: "500000"
        window: { type: calendar, unit: day, count: 1 }
        enforcement: block
        emit_at_percent: [80, 100]
    evolution: { default_rollout: next_renewal }
```

- Windows: `calendar` (timezone-aligned), `rolling` (duration since first
  use), `plan_assignment` (anchored to assignment). Allowance and bucket
  priorities share one ordering namespace (lower numbers spend first, no
  collisions); omitting `priority` preserves legacy allowance-first behavior.
- Quota `enforcement` is `block` (charge fails with `QuotaExceededError`) or
  `allow` (usage records, balance still pays). `emit_at_percent` fires
  `credits.quota_threshold` events. Features are typed definitions; plan
  feature values must match their declared type (`boolean`, `enum`, `integer`,
  `string`).
- `evolution.default_rollout` controls catalog rollout: `immediate`,
  `next_renewal`, or `new_assignments_only`. When omitted, subscription-backed
  plans use `next_renewal`, others `immediate`. (Older docs call this
  `revision_policy`; the schema and SDKs use `plan.evolution`.)

## Money as strings

All exact decimals are strings (`amount: "10000"`, `rate: "0.0025"`), matching
`^-?(?:0|[1-9]\d*)(?:\.\d+)?$`; floats are rejected. Offer prices are integer
minor units with a currency (`price: { amount_minor: 2000, currency: USD }`).

## Publishing workflow

Validate, then publish. `bursar config set` validates again, creates an
immutable version, and activates it:

```bash
bursar config validate pricing.yaml            # structural + cross-reference checks
bursar config set pricing.yaml                 # always creates a new version
```

CLI lifecycle: `validate`, `set`, `get` (active config), `list`, `activate`,
`diff <a> <b>`, `schema`, `export`, `pin`, `apply-due`; `set`/`activate` take
`--label` and `--rollout FILE` (per-release rollout manifest).

Revisions are immutable and identified by a SHA-256 digest of the canonical
document; publishing an existing digest reuses the revision. Activation
deactivates the previous revision, records history, and schedules the plan
rollout for subscribers. For staged rollouts, `bursar.catalog.*`:

| Step                            | Python                                              | TypeScript                                    |
| ------------------------------- | --------------------------------------------------- | --------------------------------------------- |
| Publish a draft (no activation) | `publish_draft(config, label)`                      | `publishDraft(config, label)`                 |
| Activate a version              | `activate(version, rollout=None)`                   | `activate(version, rollout?)`                 |
| One-shot publish + activate     | `publish_and_activate(config, label, rollout=None)` | `publishAndActivate(config, label, rollout?)` |
| Active config / public view     | `get_config()` / `public_view()`                    | `getConfig()` / `publicView()`                |

## Loading in code

The same document loads in both SDKs; the engine is stateless and database-free:

```python
from decimal import Decimal

from bursar.engine import PricingEngine
from bursar.metrics import UsageMetrics
from bursar.config import load_config_from_dict

config = load_config_from_dict(data)              # validates the whole document
engine = PricingEngine.from_dict(data)            # same validation, engine form
cost = engine.calculate(
    UsageMetrics(
        operation="completion",
        measures={
            "input_tokens": Decimal("1000"),
            "output_tokens": Decimal("500"),
        },
        dimensions={"model": "gpt-4o"},
    ),
    rate_card="standard",
)
```

```ts
import { PricingEngine } from "@zonastery/bursar";

const engine = PricingEngine.fromDict(config); // loadConfigFromDict(config) too
const cost = engine.calculate(
  {
    operation: "completion",
    measures: { input_tokens: 1000, output_tokens: 500 },
    dimensions: { model: "gpt-4o" },
  },
  { rateCard: "standard" },
);
```

`calculate_batch(metrics, rate_card=...)` / `calculateBatch(metrics,
{rateCard})` evaluates a list of events; `get_rate_card_for_plan(plan_key)`
returns the card a plan references. The engine validates measures/dimensions,
picks the first matching rule (or the `unmatched` policy), evaluates in exact
decimal, and quantizes to six decimal places with `ROUND_HALF_UP`
(0.00000775 → 0.000008). `CostBreakdown.total` is clamped to zero; negative or
non-finite costs raise.

## Common validation errors (`ConfigError`)

`ConfigError.errors()` returns Pydantic diagnostics (`loc`, `type`, `msg`);
`--json` emits them natively. Typical failures, from `validate_bursar_config`
and the schema:

- Unknown fields anywhere (`additionalProperties: false`) — e.g. a
  `billing_hook` top-level key, or an unknown `when` matcher operator.
- Money values not written as strings (floats rejected by the pattern).
- `catalog.default_plan` referencing a missing plan; a plan's `rate_card`,
  `credit_policy`, `admission_policy`, or features/quotas referencing
  undefined operations, measures, features, or buckets.
- `plans.X.credit_allowance.priority` colliding with a bucket priority; bucket
  priorities duplicated.
- `credits.grant_programs` awards or eligibility referencing unknown buckets
  or plans.
- `commerce.offers` referencing unknown plans/buckets; a topup using
  `subscription_end` expiry.
- Auto-recharge: `eligible_topups` naming a non-topup offer, quantity bounds
  not fitting the offer, mixed currencies, or `rearm_above` ≤
  `balance_below.maximum`.
- Expressions: `**`, division by zero, chained comparisons, constant formulas
  with no measure reference, or undeclared identifiers raise `ExpressionError`
  at config load.
- Engine-time: undeclared measures/dimensions on an event, missing required
  dimensions, and unmatched events on a `reject` card raise `ConfigError` at
  `calculate`.
