# 03 — Python Config Hardening (Step 4)

Resolves **H3, M4, M5, M7**, and the C3 model-side validation. All changes in
`python/src/bursar/config.py` and `python/src/bursar/interface/models.py`
unless noted. Land as Commit B (after the SQL-rename commit), independent of the
SQL contract.

---

## 3.1 H3 — Plan-reference check skipped when `plans` is None

### Evidence
`config.py:154-163` `validate_plan_references` gates the entire check on
`if plans is not None` (`config.py:158`). When `billing.subscriptions` defines
offers but `plans` is omitted, every `plan` reference is silently accepted —
and none can be valid, since no plans exist.

### Fix
`config.py:154-163`: remove the `if plans is not None` gate. When `plans` is
`None` (or empty) and `billing.subscriptions` is non-empty, reject each offer's
`plan` reference as dangling. Concretely:
```python
plans = self.plans or {}
for offer_key, offer in self.billing.subscriptions.items():
    plan_ref = offer.get("plan") if isinstance(offer, dict) else getattr(offer, "plan", None)
    if plan_ref not in plans:
        raise ConfigError(
            f"billing.subscriptions.{offer_key}.plan references unknown plan {plan_ref!r}"
        )
```
(Adjust once `BillingSection.subscriptions` is typed — see 3.2.)

---

## 3.2 M4 — Type the billing sections

### Evidence
`config.py:42-46` `BillingSection` types `subscriptions`/`topups` as
`dict[str, Any]`. `_validate_billing` (`config.py:58-82`) re-validates each
offer through a private `_BillingOffer` model then **discards** the result
(`config.py:69`), so `self.billing.subscriptions` retains raw dicts and
downstream code (`offer.get("plan")` at `config.py:160`) treats them as dicts.
A fully-typed `BillingConfig` already exists (`billing/models.py:185`).

### Fix
- `config.py:42-46`: type
  `subscriptions: dict[str, BillingOffer] = Field(default_factory=dict)` and
  `topups: dict[str, BillingCreditTopup] = Field(default_factory=dict)`,
  importing `BillingOffer`/`BillingCreditTopup` from `bursar.billing.models`.
- Delete the hand-rolled `_validate_billing`/`_BillingOffer`/`_BillingTopup`
  path (`config.py:58-82`). Pydantic now validates nested offers/topups
  (including the `grant` discriminated union from C3) with `extra="forbid"`.
- `validate_plan_references` (`config.py:154-163`): use
  `offer.plan` (attribute) instead of `offer.get("plan")`.
- Confirm `BursarConfig.billing: BillingSection` still round-trips through
  `BillingConfig.from_bursar_config` (`billing/models.py:192-215`).

---

## 3.3 C3 — `grant` discriminated union (model side)

### Fix
`billing/models.py:155-161`: replace `SubscriptionGrant` with the
`AllowanceGrant` | `CycleGrant` discriminated union (see `01-critical-fixes.md`
§C3 for the exact code). `BillingOffer.grant: Grant`. Pydantic enforces:
`cycle_grant` requires `credits`; `allowance` rejects `credits`/`bucket`/
`replace_prior`.

No `config.py` validator needed for `grant` once the union is in place — but
keep the plan-reference check (3.1) and expression checks intact.

---

## 3.4 M5 — `ConfigError` should be a `ValueError`

### Evidence
`config.py:11-13`: `class ConfigError(Exception)`. Pydantic v2 only catches
`ValueError`/`AssertionError` inside validators to wrap as `ValidationError`.
`ConfigError` raised inside `validate_structure`, `_validate_buckets`,
`_validate_metering_models_non_empty`, `validate_plan_references`, `_check_expr`
propagates **raw**, so `load_config_from_dict` callers see a mix of raw
`ConfigError` and Pydantic `ValidationError` for the same class of problem.

### Fix
`config.py:11-13`: `class ConfigError(ValueError): ...`. Verify no caller relies
on `ConfigError` not being a `ValueError` (it only broadens the type, so safe).

---

## 3.5 M7 — Redundant validation + on-model constraints

### Redundant dual checks
- `metering.models` non-empty: checked at `config.py:96-103` (before-validator)
  **and** `config.py:84-89` (field validator `_validate_metering_models_non_empty`).
  **Fix:** keep one (the before-validator at `96-103`); delete `84-89`.
- Plan `label` presence: checked at `config.py:110-116` (before-validator)
  **and** by `PlanDefinition.label: str` (`interface/models.py:143`).
  **Fix:** keep the model-level required field; delete the redundant
  before-validator block (`110-116`) — but keep the **uniqueness** check
  (`config.py:118-119`), which has no model-level equivalent.

### Move `>=0` / `>0` constraints onto the models
- `Entitlement.max_calls` (`interface/models.py:119`): change to
  `max_calls: int | None = Field(default=None, ge=0)`. Delete the manual `if`
  at `config.py:199-200` inside `_validate_plan_exprs` (which is otherwise about
  expressions — misplaced).
- `BucketDefinition.ttl_days` (`interface/models.py:156`): already has a
  model validator at `:162` (`if self.ttl_days is not None: …`). Keep the
  model-level check; delete the redundant `ttl_days > 0` re-check in
  `config.py:147-148` (the before-validator `_validate_buckets`), keeping only
  the `default`/`allow_overdraft` at-most-one and empty-map checks there.

### Result
`config.py` keeps only: structural before-validation (sections present, models
non-empty), bucket at-most-one/empty-map rules, plan-label uniqueness,
plan-reference check (H3), and expression safety. All `>=0`/`>0`/`Literal`
constraints live on the Pydantic models.

---

## 3.6 Note on `BillingSection` and `BillingConfig`

After 3.2, `BursarConfig.billing` (`BillingSection`) and the standalone
`BillingConfig` (`billing/models.py:185`) share the same offer/topup model
types. `BillingConfig.from_bursar_config` (`billing/models.py:192-215`) is the
single bridge from the pricing config to the billing runtime — confirm it still
constructs the typed `BillingOffer`/`BillingCreditTopup` (with the `grant`
union) rather than passing raw dicts through.
