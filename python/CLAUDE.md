# bursar Python SDK

Credit billing engine for AI SaaS. Calculates usage costs from expressions, manages user balances, and enforces financial-safety policy via an atomic lease lifecycle.

## Stack
Python 3.11+, Pydantic v2 (models/validation), `decimal.Decimal` for all money (no float), safe `ast`-based expression engine (no eval/exec). Optional Postgres (`psycopg2`) or Supabase backends; in-memory store for testing.

## Key source files

| File | Purpose |
|------|---------|
| `src/bursar/manager.py` | `CreditManager` — the main public API. All business logic lives here. |
| `src/bursar/interface/base.py` | `CreditStore` ABC — the interface every store must implement. |
| `src/bursar/interface/memory.py` | `MemoryStore` — reference implementation; the parity baseline for all stores. |
| `src/bursar/interface/postgres.py` | `PostgresStore` — production store; all mutations call SQL RPCs via `psycopg2`. |
| `src/bursar/interface/supabase.py` | `SupabaseStore` / `HttpxSupabaseStore` — Supabase-backed store. |
| `src/bursar/interface/models.py` | All Pydantic result types, `PlanDefinition`, `OperationPolicy`. |
| `src/bursar/engine.py` | `PricingEngine` — evaluates expression strings against `UsageMetrics`. |
| `src/bursar/manager.py` | `CreditManager` — full lifecycle: add/deduct/refund/lease/analytics. |
| `src/bursar/events.py` | `CreditEventEmitter` — typed pub/sub, 14 event types. |
| `src/bursar/metrics.py` | `UsageMetrics`, `ToolCall` — inputs to the pricing engine. |
| `src/bursar/config.py` | `PricingConfig` — validates expression strings at load time. |
| `src/bursar/sql/` | Numbered SQL migrations, one per feature domain (`001_core_schema.sql` → `012_feature_limits.sql`). `009_deduct_and_leases.sql` has the atomic deduct + full lease lifecycle; `010_credit_tiers.sql` adds configurable credit tiers (priority-ordered balance buckets); `011_lazy_expiry.sql` adds lazy per-user credit expiry + idempotent `add_credits`; `012_feature_limits.sql` adds the `check_feature_limit` RPC for per-feature invocation-count limits (ledger-derived count over `credit_transactions`, no new table — `011_feature_limits.sql` was unavailable since `011_lazy_expiry.sql` already claimed that slot). All idempotent — `setup()` re-applies every file on every run. |
| `src/bursar/__init__.py` | Package exports — everything users `import from bursar`. |

## Architecture

```
CreditManager
  ├── PricingEngine          (calculate cost from UsageMetrics)
  ├── CreditStore            (ABC — memory / postgres / supabase)
  │     ├── deduct_with_allowance()   atomic: allowance→cap→floor→debit (internal core)
  │     ├── create_lease / settle_lease / release_lease / renew_lease
  │     └── ... (30+ abstract methods)
  └── CreditEventEmitter     (optional pub/sub)
```

**Hot path — immediate charge:** `manager.deduct()` → `store.deduct_with_allowance()` (one atomic SQL RPC).

**Safe path — lease lifecycle:** `manager.reserve()` → do work → `manager.settle()` or `manager.release()`. Admission is the only gate; `settle` is de-clamped (bills full actual cost). Use `manager.run_billed()` as a one-call shortcut.

**Financial-safety presets** (constructor `policy=`):
- `strict_prepaid` (default) — floor ≥ 0, holds sized at worst case, structurally zero debt.
- `overdraft` — negative `overdraft_floor`, bills full actual at settle, bounded admission.

**Policy resolution** (most specific wins): per-call `billing_mode` → `plan.per_operation[type]` → `plan.default_billing_mode` → constructor preset. Planless users always get the constructor preset (never unlimited).

## Money invariants
- All amounts are `decimal.Decimal`; never `float`.
- Stored as `NUMERIC(18,4)` in Postgres; quantized with `ROUND_HALF_UP`.
- Both Python and JS round identically — same config bills the same amount.

## Tests

| File | What it covers |
|------|----------------|
| `tests/test_store.py` | MemoryStore unit tests (parity baseline) |
| `tests/test_manager.py` | CreditManager happy-path and error cases |
| `tests/test_lease.py` | Lease lifecycle (27 tests) |
| `tests/test_lease_adversarial.py` | Concurrency, idempotency (31 tests) |
| `tests/test_tiers.py` | Credit tiers — happy-path priority walk, refund LIFO, expiry, overdraft sink |
| `tests/test_tiers_adversarial.py` | Credit tiers — concurrency, idempotent replay, config drift |
| `tests/test_store_integration.py` | Real Postgres tests, incl. `CreditManager` end-to-end tier coverage. The 7 real-Postgres concurrency tests are `@pytest.mark.repeat(5)` — money-critical races, rerun to surface rare interleavings |
| `tests/test_security_rls.py` | RLS/privilege lockdown against real Postgres roles (`anon`/`authenticated`/`service_role`) — the REVOKE/RLS checks the rest of the suite bypasses by connecting as a superuser |
| `tests/test_invariants_property.py` | Hypothesis stateful property test — ledger conservation across grant/deduct/lease/refund sequences, run once strict-prepaid and once overdraft |
| `tests/test_engine.py` | PricingEngine expression evaluation |

Run: `pytest python/tests/`. Real-Postgres tests resolve a DSN from `DATABASE_URL` → `BURSAR_TEST_PG_URL` → a testcontainers-managed `postgres:16` (Docker permitting) → skip; see `tests/conftest.py`.

Linting: `ruff check python/src/ python/tests/` — max line length 120, complexity ≤ 15.
Types: `pyright python/src/`.

## Parity rule
`MemoryStore` is the reference implementation. Any change to store behavior must be replicated across `PostgresStore`, `SupabaseStore`, and the JS `MemoryStore`. New abstract methods go in `base.py` first.
