# bursar Python SDK

Credit billing engine for AI SaaS. Calculates usage costs from expressions, manages user balances, enforces financial-safety policy via an atomic lease lifecycle, and handles provider billing (Stripe, Dodo) through a unified event-driven billing subsystem.

## Stack
Python 3.11+, Pydantic v2 (models/validation), `decimal.Decimal` for all money (no float), safe `ast`-based expression engine (no eval/exec). Optional Postgres (`psycopg2`) backend; in-memory store for testing. Stripe/Dodo provider integrations in `providers/`.

## Key source files

| File | Purpose |
|------|---------|
| `src/bursar/manager.py` | `CreditManager` — the main public API. All business logic lives here. |
| `src/bursar/interface/base.py` | `CreditStore` ABC — the interface every store must implement. |
| `src/bursar/interface/postgres.py` | `PostgresStore` — production store; all mutations call SQL RPCs via `psycopg2`. |
| `src/bursar/interface/models.py` | All Pydantic result types, `PlanDefinition`, `OperationPolicy`. |
| `src/bursar/engine.py` | `PricingEngine` — evaluates expression strings against `UsageMetrics`. |
| `src/bursar/events.py` | `CreditEventEmitter` — typed pub/sub, 19 event types. |
| `src/bursar/metrics.py` | `UsageMetrics`, `ToolCall` — inputs to the pricing engine. |
| `src/bursar/config.py` | `BursarConfig` — validates expression strings at load time. |
| `src/bursar/expr.py` | Safe `ast`-based expression evaluator for pricing formulas. |
| `src/bursar/billing/manager.py` | `BillingManager` — provider-agnostic billing orchestration. |
| `src/bursar/billing/postgres.py` | `PostgresBillingStore` — billing state persistence via `psycopg2`. |
| `src/bursar/billing/store.py` | `BillingStore` ABC — interface for billing persistence. |
| `src/bursar/billing/models.py` | Billing Pydantic models: events, subscriptions, invoices, payments, offers, topups. |
| `src/bursar/providers/` | Stripe and Dodo webhook→event mappers and provider wrappers. |
| `src/bursar/repositories/` | Data-access layer (balance, bucket, lease, deduction, plan, pricing, team, analytics, billing sub-repos). |

## Architecture

```
CreditManager
  ├── PricingEngine          (calculate cost from UsageMetrics)
  ├── CreditStore            (ABC — memory / postgres)
  │     ├── deduct_with_allowance()   atomic: allowance→cap→floor→debit (internal core)
  │     ├── create_lease / settle_lease / release_lease / renew_lease
  │     └── ... (30+ abstract methods)
  └── CreditEventEmitter     (optional pub/sub)

BillingManager
  ├── ProviderMapper         (Stripe / Dodo webhook → BillingEvent)
  ├── BillingStore           (ABC — postgres)
  └── BillingEventEmitter    (typed pub/sub, 35+ event types)
```

**Hot path — immediate charge:** `manager.deduct()` → `store.deduct_with_allowance()` (one atomic SQL RPC).

**Safe path — lease lifecycle:** `manager.reserve()` → do work → `manager.settle()` or `manager.release()`. Admission is the only gate; `settle` is de-clamped (bills full actual cost). Use `manager.run_billed()` as a one-call shortcut.

**Financial-safety presets** (constructor `policy=`):
- `strict_prepaid` (default) — floor ≥ 0, holds sized at worst case, structurally zero debt.
- `overdraft` — negative `overdraft_floor`, bills full actual at settle, bounded admission.

**Policy resolution** (most specific wins): per-call `billing_mode` → `plan.per_operation[type]` → `plan.billing_mode` → constructor preset. Planless users always get the constructor preset (never unlimited).

## Money invariants
- All amounts are `decimal.Decimal`; never `float`.
- Stored as `NUMERIC(18,4)` in Postgres; quantized with `ROUND_HALF_UP`.
- Both Python and JS round identically — same config bills the same amount.

## Tests

| File | What it covers |
|------|----------------|
| `tests/test_allowance.py` | Allowance window resolution |
| `tests/test_config.py` | Config validation edge cases |
| `tests/test_config_parity.py` | Config loading parity with JavaScript SDK |
| `tests/test_engine.py` | PricingEngine expression evaluation |
| `tests/test_expr.py` | Expression parser/evaluator edge cases |
| `tests/test_security_rls.py` | RLS/privilege lockdown against real Postgres roles (`anon`/`authenticated`/`service_role`) — the REVOKE/RLS checks the rest of the suite bypasses by connecting as a superuser |
| `tests/test_store_integration.py` | Real Postgres tests, incl. `CreditManager` end-to-end tier coverage. The 7 real-Postgres concurrency tests are `@pytest.mark.repeat(5)` — money-critical races, rerun to surface rare interleavings |

Run: `pytest python/tests/`. Real-Postgres tests resolve a DSN from `DATABASE_URL` → `BURSAR_TEST_PG_URL` → a testcontainers-managed `postgres:16` (Docker permitting) → skip; see `tests/conftest.py`.

Linting: `ruff check python/src/ python/tests/` — max line length 120, complexity ≤ 15.
Types: `pyright python/src/`.
