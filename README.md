# ducto

Declarative credit calculation engine for AI SaaS platforms.

Define pricing in YAML — a safe AST-walking expression engine calculates
credit costs from usage metrics. Supports per-model formulas, tool costs,
search/RAG pricing, cache discounts, and fixed-cost batch jobs.

## Features

- **Safe expression engine** — Uses Python's `ast` module with a strict
  allowlist (no `eval()` of raw strings, no `exec()`, no attribute access,
  no imports). Validated at config load time.
- **YAML configuration** — Human-readable pricing definitions validated by
  Pydantic.
- **Multi-dimensional** — Per-model formulas (with `_default` fallback),
  per-tool overrides, search/RAG, cache read discounts, fixed-cost jobs.
- **Stateless core** — Pure calculation layer has zero database dependency.
- **Auditable** — Returns a structured `CostBreakdown` with per-dimension
  costs and metadata.
- **Pluggable storage** — Reserve-then-deduct pattern via `CreditStore`
  adapters: Supabase, raw PostgreSQL, or in-memory for testing.

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

Requires Python ≥ 3.11.

## Quick Start

```python
from ducto import PricingEngine, UsageMetrics, ToolCall

engine = PricingEngine.from_yaml("pricing.yaml")

result = engine.calculate(UsageMetrics(
    model="gpt-4",
    input_tokens=342,
    output_tokens=1204,
    tool_calls=[ToolCall(name="web_search")],
    web_search_calls=1,
    search_queries=2,
    search_results=10,
))

print(f"Total credits: {result.total}")
print(f"  Model: {result.model_credits}")
print(f"  Tools: {result.tool_credits}")
print(f"  Search: {result.search_credits}")
```

## Pricing Configuration

Define pricing in YAML (see [`tests/fixtures/pricing_full.yaml`](tests/fixtures/pricing_full.yaml)
for a complete example):

```yaml
version: 1

models:
  claude-opus-4: "input_tokens * 0.005 + output_tokens * 0.015"
  _default: "input_tokens * 0.001 + output_tokens * 0.003"

tools:
  _default: "tool_calls * 0"
  web_search: "web_search_calls * 0.5"
  code_exec: "code_exec_calls * 0.3"

search:
  costs: "search_queries * 0.5 + search_results * 0.05"

cache:
  discount: "-cache_read_tokens * 0.0045"

fixed:
  roadmap_gen: 20
  topic_gen: 10

min_balance: 5
```

### Available expression variables

| Variable             | Source field in `UsageMetrics` |
| -------------------- | ------------------------------ |
| `input_tokens`       | `metrics.input_tokens`         |
| `output_tokens`      | `metrics.output_tokens`        |
| `cache_read_tokens`  | `metrics.cache_read_tokens`    |
| `cache_write_tokens` | `metrics.cache_write_tokens`   |
| `tool_calls`         | `len(metrics.tool_calls)`      |
| `search_queries`     | `metrics.search_queries`       |
| `search_results`     | `metrics.search_results`       |
| `web_search_calls`   | `metrics.web_search_calls`     |
| `code_exec_calls`    | `metrics.code_exec_calls`      |

### Supported functions in expressions

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
from supabase import create_client
from ducto.interface.supabase import SupabaseStore

client = create_client(url, service_role_key)
store = SupabaseStore(client=client)
```

### PostgresStore

```python
from ducto.interface.postgres import PostgresStore

store = PostgresStore("postgresql://user:pass@host:5432/db")
```

### Custom adapters

Implement `ducto.interface.base.CreditStore` (an ABC with
8 methods) to integrate with any backend.

## Credit Lifecycle

`CreditManager` orchestrates a three-step reserve-then-deduct pattern:

1. **Calculate** — `PricingEngine.calculate(UsageMetrics)` → `CostBreakdown`
2. **Reserve** — `store.reserve_credits(user_id, amount)` → `ReserveResult`
   (locks the user row; reservations auto-expire after 10 minutes)
3. **Deduct** — `store.deduct_credits(user_id, reservation_id, amount)`
   → `DeductionResult` (idempotent, atomic)

```python
manager = CreditManager(store=store)

# One-time setup: runs bundled SQL migrations
manager.setup()

# Load pricing from YAML
manager.load_pricing_from_yaml("pricing.yaml")

# Deduct credits for a usage event
result = manager.deduct(
    user_id="user_abc",
    metrics=UsageMetrics(model="claude-opus-4", input_tokens=500, output_tokens=200),
    idempotency_key="chat_42_turn_7",
)
```

## SQL Migrations

Three bundled SQL files create the required schema:

| File                     | Creates                                                                                                              |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `001_credit_tables.sql`  | `user_credits`, `credit_transactions`, `credit_reservations` tables, RLS policies, signup bonus trigger              |
| `002_credit_rpcs.sql`    | `credits_add`, `reserve_credits`, `deduct_credits`, `get_credits_balance` RPCs (SECURITY DEFINER, service_role only) |
| `003_pricing_config.sql` | `credit_pricing_config` table, `get_active_pricing_config`, `set_active_pricing_config` RPCs                         |

All DDL is idempotent (uses `IF NOT EXISTS` / `CREATE OR REPLACE`).

## Expression Safety

The expression engine uses a strict AST-walking validator:

1. Parse `ast.parse(expr, mode="eval")`
2. Walk the AST — every node type must be in an allowlist (~25 node types:
   binary ops, comparisons, conditionals, booleans, constants, names, calls)
3. Function calls must be in a whitelist (`ceil`, `floor`, `min`, `max`,
   `round`)
4. Rejects: attributes (`x.__class__`), subscripts (`x[0]`), lambdas,
   comprehensions, imports, starred expressions
5. Evaluation namespace has `__builtins__` emptied — only the 5 whitelisted
   math/python builtins and user-provided variable names are available
6. All expression strings are validated at config load time —
   invalid configs never reach the engine

## Architecture

```
ducto/
├── expr.py          # Safe AST expression evaluator
├── config.py        # Pydantic model + YAML loading for PricingConfig
├── engine.py        # PricingEngine — core calculation logic
├── metrics.py       # UsageMetrics, ToolCall dataclasses
├── breakdown.py     # CostBreakdown dataclass
├── manager.py       # CreditManager — orchestrates calculate→reserve→deduct
└── interface/
    ├── base.py      # CreditStore ABC
    ├── models.py    # Pydantic schemas for store operations
    ├── memory.py    # MemoryStore (in-memory for testing)
    ├── supabase.py  # SupabaseStore adapter
    └── postgres.py  # PostgresStore adapter
sql/
    ├── 001_credit_tables.sql
    ├── 002_credit_rpcs.sql
    └── 003_pricing_config.sql
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

## License

MIT © [Apurv Wagh](LICENSE)
