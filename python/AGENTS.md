# bursar Python SDK

Credit billing engine for AI SaaS. Calculates usage costs from expressions, manages user balances, enforces financial-safety policy via an atomic lease lifecycle, and handles provider billing (Stripe, Dodo) through a unified event-driven billing subsystem.

## Stack
Python 3.12+, Pydantic v2 (models/validation), `decimal.Decimal` for all money (no float), safe `ast`-based expression engine (no eval/exec). Optional Postgres (`psycopg2`) backend — `PostgresStore` is the only store. Stripe/Dodo provider integrations in `providers/`.

## Key source files

| File | Purpose |
|------|---------|
| `src/bursar/credits/service.py` | Internal credit capability (`CreditsService`) owned by the `Bursar` facade. |
| `src/bursar/credits/store.py` | `CreditStore` ABC — the interface every store must implement. |
| `src/bursar/credits/postgres/store.py` | `PostgresStore` — production store; all mutations call SQL RPCs via `psycopg2`. |
| `src/bursar/credits/types/` | Pydantic result types and policy types (`OperationPolicy`, `PlanCreditPolicy`, ...). `PlanDefinition` lives in `config/`. |
| `src/bursar/engine.py` | `PricingEngine` — evaluates expression strings against `UsageMetrics`. |
| `src/bursar/credits/events.py` | `CreditEventEmitter` — typed pub/sub for credit lifecycle events. |
| `src/bursar/metrics.py` | `UsageMetrics`, `ToolCall` — inputs to the pricing engine. |
| `src/bursar/config/` | `BursarConfig` (`config/types.py`) — validates expression strings at load time. |
| `src/bursar/expr/` | Safe `ast`-based expression evaluator for pricing formulas. |
| `src/bursar/billing/billing_service.py` | Provider-agnostic billing orchestration owned by `Bursar`. |
| `src/bursar/billing/postgres/store.py` | `PostgresBillingStore` — billing state persistence via `psycopg2`. |
| `src/bursar/billing/billing_store.py` | `BillingStore` ABC — interface for billing persistence. |
| `src/bursar/billing/types/` | Billing Pydantic models: events, subscriptions, invoices, payments, offers, topups. |
| `src/bursar/providers/` | Stripe and Dodo webhook→event mappers and provider wrappers. |
| `src/bursar/credits/postgres/repositories/` | Data-access layer (balance, bucket, lease, deduction, plan, pricing, team, analytics). |

## Architecture

```
Bursar facade
  ├── PricingEngine          (calculate cost from UsageMetrics)
  ├── CreditStore            (ABC — postgres only)
  │     ├── deduct_with_allowance()   atomic: allowance→cap→floor→debit (internal core)
  │     ├── create_lease / settle_lease / release_lease / renew_lease
  │     └── ... (30+ abstract methods)
  └── CreditEventEmitter     (optional pub/sub)

BillingService
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
- Stored as `numeric(20,6)` in Postgres; quantized to 6dp with `ROUND_HALF_UP`.
- Both Python and JS round identically — same config bills the same amount.

## Tests

| File | What it covers |
|------|----------------|
| `tests/test_config.py` | Config validation edge cases |
| `tests/test_config_parity.py` | Config loading parity with JavaScript SDK |
| `tests/test_engine.py` | PricingEngine expression evaluation |
| `tests/test_expr.py` | Expression parser/evaluator edge cases |
| `tests/test_security_rls.py` | Tenant-scoped SDK smoke test through the restricted RPC role |
| `tests/test_multitenancy.py` | Cross-tenant isolation, suspended tenants, role boundaries, and partition RLS |
| `tests/test_sql_migration_contract.py` | Migration ledger, role/RPC privileges, forced RLS, keys, and index invariants |
| `tests/test_sql_storage_lifecycle.py` | Bounded retention, partition maintenance, and lock-contention behavior |
| `tests/test_store_integration.py` | Public credit workflows plus transactional, replay, and migration-race regressions |

Run: `pytest python/tests/`. Real-Postgres tests use a testcontainers-managed PostgreSQL 17 + pg_partman 5 + pg_jsonschema 0.3 instance by default. An external `DATABASE_URL` requires `BURSAR_ALLOW_DATABASE_RESET=1`; see `tests/conftest.py`.

Linting: `ruff check python/src/ python/tests/ python/scripts/` and `ruff format --check` — max line length 120, complexity ≤ 15.
Types: `pyright python/src/`.
