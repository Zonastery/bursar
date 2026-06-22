# ducto

[![CI](https://github.com/apoorwv/ducto/actions/workflows/ci.yml/badge.svg)](https://github.com/apoorwv/ducto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Add usage-based credits to your AI SaaS in minutes — not weeks.

ducto is a drop-in credit calculation engine. Define pricing as math expressions
(per-model, per-tool, search/RAG, cache, fixed jobs), connect a database, and
start deducting credits. No billing infrastructure to build. No YAML configs.
Pricing lives in your DB — update it live without redeploys.

```python
from ducto import CreditManager, UsageMetrics
from ducto.interface.supabase import HttpxSupabaseStore

# 3 lines. Your users now have credits.
store = HttpxSupabaseStore(url=supabase_url, key=service_role_key)
manager = CreditManager(store=store)
manager.load_pricing_from_store()

# Deduct credits from a single LLM call
manager.deduct(
    user_id="user_abc",
    metrics=UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200),
    idempotency_key="chat_42_turn_7",
)
```

Run `ducto migrate "postgresql://..." && ducto pricing set config.json` and
you're live. Balance tracking, idempotent deductions, overuse protection —
all handled.

## Features

- **Safe expression engine** — Uses Python's `ast` module with a strict
  allowlist (no `eval()` of raw strings, no `exec()`, no attribute access,
  no imports). Validated at config load time.
- **Database-backed pricing** — Pricing expressions stored in a
  `credit_pricing_config` table. Enables live pricing updates without
  redeploys. Dict loading available for testing and stateless calculation.
- **Multi-dimensional** — Per-model formulas (with `_default` fallback),
  per-tool overrides, search/RAG, cache read discounts, fixed-cost jobs.
- **Stateless core** — Pure calculation layer has zero database dependency.
- **Auditable** — Returns a structured `CostBreakdown` with per-dimension
  costs and metadata.
- **Pluggable storage** — Reserve-then-deduct pattern via `CreditStore`
  adapters: Supabase, raw PostgreSQL, or in-memory for testing.
- **Safe defaults** — Configurable `min_balance` floor, reservation expiry
  (10 min), non-negative balance enforcement at the database level.

## Installation

```bash
pip install ducto

# With Supabase store support
pip install "ducto[supabase]"

# With PostgreSQL store support
pip install "ducto[postgres]"

# Development & testing
pip install "ducto[test]"
```

Requires Python 3.11+.

## Quick Start

### 1. Install and migrate

```bash
pip install "ducto[postgres]"
ducto migrate "postgresql://user:pass@host:5432/db"
```

Creates all tables (`user_credits`, `credit_transactions`,
`credit_reservations`) and RPCs — idempotent, safe to run on every deploy.

### 2. Seed pricing

```bash
ducto pricing set - <<'JSON'
{
  "version": 1,
  "models": {
    "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
    "_default": "input_tokens * 0.001 + output_tokens * 0.003"
  }
}
JSON
```

Pricing is stored in `credit_pricing_config`. Update it at any time — no
redeploy needed.

### 3. Deduct credits

```python
from ducto import CreditManager, UsageMetrics
from ducto.interface.postgres import PostgresStore

store = PostgresStore("postgresql://user:pass@host:5432/db")
manager = CreditManager(store=store)
manager.load_pricing_from_store()

manager.add_credits("user_abc", 1000)

result = manager.deduct(
    user_id="user_abc",
    metrics=UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200),
    idempotency_key="chat_session_42",
)
print(f"Deducted {abs(result.amount)} credits. Balance: {result.balance_after}")
```

### Calculation only (no database)

For testing or stateless calculation without a store:

```python
from ducto import PricingEngine, UsageMetrics

engine = PricingEngine.from_dict({
    "version": 1,
    "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
})

result = engine.calculate(
    UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200),
)
print(f"Total credits: {result.total}")
```

## Pricing Configuration

Pricing is stored in the `credit_pricing_config` table via the
`set_active_pricing_config` RPC. The
`CreditManager.load_pricing_from_store()` method fetches the active
config at runtime. See [`scripts/seed_pricing.py`](scripts/seed_pricing.py)
for reference.

### Expression format

```json
{
  "version": 1,
  "models": {
    "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
    "_default": "input_tokens * 0.001 + output_tokens * 0.003"
  },
  "tools": {
    "_default": "tool_calls * 0",
    "web_search": "web_search_calls * 0.5"
  },
  "search": {
    "costs": "search_queries * 0.5 + search_results * 0.05"
  },
  "cache": {
    "discount": "-cache_read_tokens * 0.0045"
  },
  "fixed": {
    "batch_job": 20
  },
  "min_balance": 5
}
```

### Available expression variables

| Variable | Source field in `UsageMetrics` |
|----------|--------------------------------|
| `input_tokens` | `metrics.input_tokens` |
| `output_tokens` | `metrics.output_tokens` |
| `cache_read_tokens` | `metrics.cache_read_tokens` |
| `cache_write_tokens` | `metrics.cache_write_tokens` |
| `tool_calls` | `len(metrics.tool_calls)` |
| `search_queries` | `metrics.search_queries` |
| `search_results` | `metrics.search_results` |
| `web_search_calls` | `metrics.web_search_calls` |
| `code_exec_calls` | `metrics.code_exec_calls` |

### Supported functions

`ceil`, `floor`, `min`, `max`, `round`

### Version 1 rules

- `models` section is **required** and must be a non-empty dict
- `_default` model is used when no specific model matches
- Tool costs don't double-count: tools with individual entries are
  evaluated separately; remaining calls use `_default`
- `cache.discount` is typically a negative value (savings/rebate)
- `fixed` costs are non-negative integers, applied when
  `UsageMetrics.fixed_job` matches

## Storage Backends

### MemoryStore (testing/dev)

```python
from ducto import CreditManager
from ducto.interface.memory import MemoryStore

store = MemoryStore()
manager = CreditManager(store=store)
```

### SupabaseStore

```python
from ducto.interface.supabase import HttpxSupabaseStore

store = HttpxSupabaseStore(url=supabase_url, key=service_role_key)
```

### PostgresStore

```python
from ducto.interface.postgres import PostgresStore

store = PostgresStore("postgresql://user:pass@host:5432/db")
```

### Custom adapters

Implement `ducto.interface.base.CreditStore` (an ABC with 8 methods) to
integrate with any backend.

## Credit Lifecycle

`CreditManager` orchestrates a three-step reserve-then-deduct pattern:

1. **Calculate** — `PricingEngine.calculate(UsageMetrics)` -> `CostBreakdown`
2. **Reserve** — `store.reserve_credits(user_id, amount)` -> `ReserveResult`
   (locks the user row; reservations auto-expire after 10 minutes)
3. **Deduct** — `store.deduct_credits(user_id, reservation_id, amount)`
   -> `DeductionResult` (idempotent, atomic)

```python
manager = CreditManager(store=store)
manager.load_pricing_from_store()
result = manager.deduct(
    user_id="user_abc",
    metrics=UsageMetrics(model="gpt-4", input_tokens=100, output_tokens=50),
    idempotency_key="tx_42",
)
```

Pricing can also be loaded from a dict (no database):

```python
manager.publish_pricing_from_dict({
    "version": 1,
    "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
})
```

## SQL Migrations

Three bundled SQL files create the required schema:

| File | Creates |
|------|---------|
| `001_credit_tables.sql` | `user_credits`, `credit_transactions`, `credit_reservations` tables, RLS policies, signup bonus trigger |
| `002_credit_rpcs.sql` | `credits_add`, `reserve_credits`, `deduct_credits`, `get_credits_balance` RPCs (SECURITY DEFINER, service_role only) |
| `003_pricing_config.sql` | `credit_pricing_config` table, `get_active_pricing_config`, `set_active_pricing_config` RPCs |

All DDL is idempotent (uses `IF NOT EXISTS` / `CREATE OR REPLACE`).

### CLI reference

```bash
# Create tables, indexes, and RPC functions
ducto migrate "postgresql://user:pass@host:5432/db"

# Show current active pricing config
ducto pricing get

# Update active pricing from a JSON or YAML file
ducto pricing set config.yaml
```

The `pricing` commands require `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`
environment variables.

Or from Python:

```python
from ducto.interface.supabase import run_migrations

result = run_migrations("postgresql://user:pass@host:5432/db")
assert result.success, result.errors
```

## Expression Safety

The expression engine uses a strict AST-walking validator:

1. Parse `ast.parse(expr, mode="eval")`
2. Walk the AST -- every node type must be in an allowlist (~25 node
   types: binary ops, comparisons, conditionals, booleans, constants,
   names, calls)
3. Function calls must be in a whitelist (`ceil`, `floor`, `min`, `max`,
   `round`)
4. Rejects: attributes (`x.__class__`), subscripts (`x[0]`), lambdas,
   comprehensions, imports, starred expressions
5. Evaluation namespace has `__builtins__` emptied -- only the 5
   whitelisted math/python builtins and user-provided variable names are
   available
6. All expression strings are validated at config load time -- invalid
   configs never reach the engine

## Architecture

```
ducto/
  expr.py          # Safe AST expression evaluator
  config.py        # Pydantic model + dict loading for PricingConfig
  engine.py        # PricingEngine -- core calculation logic
  metrics.py       # UsageMetrics, ToolCall dataclasses
  breakdown.py     # CostBreakdown dataclass
  manager.py       # CreditManager -- calculate -> reserve -> deduct
  interface/
    base.py        # CreditStore ABC
    models.py      # Pydantic schemas for store operations
    memory.py      # MemoryStore (in-memory for testing)
    supabase.py    # HttpxSupabaseStore adapter + run_migrations()
    postgres.py    # PostgresStore adapter
  sql/
    001_credit_tables.sql
    002_credit_rpcs.sql
    003_pricing_config.sql
```

## Development

```bash
# Install with dev dependencies
pip install "ducto[test]"

# Run tests
pytest

# Lint & format
ruff check .
ruff format .

# Type check
pyright
```

