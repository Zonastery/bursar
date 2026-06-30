# ducto Python SDK

Credit billing engine for AI SaaS. Calculates usage costs from expressions, manages user balances, and enforces financial-safety policy via an atomic lease lifecycle.

## Stack
Python 3.11+, Pydantic v2 (models/validation), `decimal.Decimal` for all money (no float), safe `ast`-based expression engine (no eval/exec). Optional Postgres (`psycopg2`) or Supabase backends; in-memory store for testing.

## Key source files

| File | Purpose |
|------|---------|
| `src/ducto/manager.py` | `CreditManager` — the main public API. All business logic lives here. |
| `src/ducto/interface/base.py` | `CreditStore` ABC — the interface every store must implement. |
| `src/ducto/interface/memory.py` | `MemoryStore` — reference implementation; the parity baseline for all stores. |
| `src/ducto/interface/postgres.py` | `PostgresStore` — production store; all mutations call SQL RPCs via `psycopg2`. |
| `src/ducto/interface/supabase.py` | `SupabaseStore` / `HttpxSupabaseStore` — Supabase-backed store. |
| `src/ducto/interface/models.py` | All Pydantic result types, `PlanDefinition`, `OperationPolicy`. |
| `src/ducto/engine.py` | `PricingEngine` — evaluates expression strings against `UsageMetrics`. |
| `src/ducto/manager.py` | `CreditManager` — full lifecycle: add/deduct/refund/lease/analytics. |
| `src/ducto/events.py` | `CreditEventEmitter` — typed pub/sub, 14 event types. |
| `src/ducto/metrics.py` | `UsageMetrics`, `ToolCall` — inputs to the pricing engine. |
| `src/ducto/config.py` | `PricingConfig` — validates expression strings at load time. |
| `src/ducto/sql/` | Numbered SQL migrations (`001_…` → `016_…`). `016` adds the lease lifecycle. |
| `src/ducto/__init__.py` | Package exports — everything users `import from ducto`. |

## Architecture

```
CreditManager
  ├── PricingEngine          (calculate cost from UsageMetrics)
  ├── CreditStore            (ABC — memory / postgres / supabase)
  │     ├── deduct_with_allowance()   atomic: allowance→cap→floor→debit
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
| `tests/test_lease_adversarial.py` | Concurrency, fuzz, idempotency (31 tests) |
| `tests/test_store_integration.py` | Real Postgres tests (requires `PG_TEST_DSN`) |
| `tests/test_engine.py` | PricingEngine expression evaluation |

Run: `pytest python/tests/` (unit) or `PG_TEST_DSN=... pytest python/tests/test_store_integration.py` (integration).

Linting: `ruff check python/src/ python/tests/` — max line length 120, complexity ≤ 15.
Types: `pyright python/src/`.

## Parity rule
`MemoryStore` is the reference implementation. Any change to store behavior must be replicated across `PostgresStore`, `SupabaseStore`, and the JS `MemoryStore`. New abstract methods go in `base.py` first.
