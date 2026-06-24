# ducto

[![CI](https://github.com/apoorwv/ducto/actions/workflows/ci.yml/badge.svg)](https://github.com/apoorwv/ducto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)(LICENSE)

Add usage-based credits to your AI SaaS in minutes — not weeks.

ducto is a drop-in credit calculation engine. Define pricing as math expressions
(per-model, per-tool, search/RAG, cache, fixed jobs), connect a database, and
start deducting credits. No billing infrastructure to build. Pricing lives in
your DB — update it live without redeploys.

```python
from ducto import CreditManager, UsageMetrics
from ducto.interface.supabase import HttpxSupabaseStore

store = HttpxSupabaseStore(url=supabase_url, key=service_role_key)
manager = CreditManager(store=store)
manager.load_pricing_from_store()

manager.add_credits("user_abc", 1000)

result = manager.deduct(
    user_id="user_abc",
    metrics=UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200),
    idempotency_key="chat_42",
)
print(f"Deducted {abs(result.amount)} credits. Balance: {result.balance_after}")
```

## Features

- **Safe expression engine** — Python `ast` module with strict allowlist. `min`, `max`, `if`, `tier`, `clamp`, `ceil`, `floor`, `round`, `percentile`. No eval/exec, no attribute access, no imports.
- **Plan-based pricing** — Subscription plans with free monthly allowances, rate overrides, and feature flags. Allowance consumed before balance.
- **Refunds** — Full and partial credit reversals with duplicate detection and idempotency.
- **Credit expiry / TTL** — Time-bound credits with `expires_at` on `add_credits`. Sweep with dry-run mode.
- **Team / shared balances** — Separate team credit pools with per-member spend caps and attribution.
- **Spend caps** — Per-user daily/monthly limits with `deny`, `warn`, `notify` actions. Per-model caps supported.
- **Usage analytics** — `spend_by_user`, `spend_by_model`, `top_users`, `daily_spend`, `aggregate_stats` across time windows.
- **Event hooks** — Typed pub/sub for `credits.deducted`, `credits.added`, `credits.refunded`, `credits.expired`, `credits.cap_reached`, `credits.cap_warning`, `credits.low_balance`.
- **Database-backed pricing** — Live updates without redeploys. Dict loading for testing.
- **Multi-dimensional** — Per-model (with `_default` fallback), per-tool overrides, search/RAG, cache discounts, fixed-cost jobs.
- **Pluggable storage** — Reserve-then-deduct via `CreditStore` adapters: Supabase, PostgreSQL, in-memory.
- **Safe defaults** — `min_balance` floor, reservation expiry (10 min), idempotent deductions, concurrent protection.
- **Auditable** — Structured `CostBreakdown` with per-dimension costs.

## Installation

```bash
pip install ducto

# With Supabase store
pip install "ducto[supabase]"

# With PostgreSQL store
pip install "ducto[postgres]"

# Development & testing
pip install "ducto[test]"
```

Requires Python 3.11+.

## Full docs

**[apoorwv.github.io/ducto](https://apoorwv.github.io/ducto/)** — Python API reference, expressions, configuration, examples.

## Quick Start

### 0. Stateless calculation (no database)

```python
from ducto import PricingEngine, UsageMetrics

engine = PricingEngine.from_dict({
    "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
})

result = engine.calculate(UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
print(f"Total credits: {result.total}")
```

### 1. Install and migrate

```bash
pip install "ducto[postgres]"
ducto migrate "postgresql://user:pass@host:5432/db"
```

Creates all tables (`user_credits`, `credit_transactions`, `credit_reservations`,
`credit_plans`, `credit_usage_window`, `credit_teams`, `credit_team_members`,
`credit_spend_caps`, `credit_pricing_config`) and 20+ RPCs — all idempotent.

### 2. Seed pricing

```bash
ducto pricing set - <<'JSON'
{
  "models": { "_default": "input_tokens * 0.01 + output_tokens * 0.03" },
  "plans": {
    "free": { "id": "free", "name": "Free Tier", "free_allowance": 50000 },
    "pro": { "id": "pro", "name": "Pro", "free_allowance": 500000 }
  }
}
JSON
```

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
    idempotency_key="tx_001",
)
print(f"Deducted {abs(result.amount)} credits. Balance: {result.balance_after}")
```

## Pricing Configuration

### Basic config

```json
{
  "models": {
    "gpt-4": "input_tokens * 0.01 + output_tokens * 0.03",
    "_default": "input_tokens * 0.001 + output_tokens * 0.003"
  },
  "tools": { "_default": "tool_calls * 0" },
  "search": { "costs": "search_queries * 0.5 + search_results * 0.05" },
  "cache": { "discount": "-cache_read_tokens * 0.0045" },
  "fixed": { "batch_job": 20 },
  "min_balance": 5
}
```

### With plans

```json
{
  "models": { "_default": "input_tokens * 0.01 + output_tokens * 0.03" },
  "plans": {
    "free": {
      "id": "free",
      "name": "Free Tier",
      "free_allowance": 50000,
      "rate_overrides": { "_default": "input_tokens * 0.02 + output_tokens * 0.06" },
      "features": { "max_concurrency": 1 }
    },
    "pro": {
      "id": "pro",
      "name": "Pro Plan",
      "free_allowance": 500000
    }
  }
}
```

## Feature Examples

### Refunds

```python
tx = manager.deduct("user_abc", UsageMetrics(model="gpt-4", input_tokens=500))
refund = manager.refund_credits(tx.transaction_id)                     # full refund
partial = manager.refund_credits(tx.transaction_id, amount=5)          # partial
```

### Credit expiry

```python
manager.add_credits("user_abc", 100, "purchase", expires_at=datetime(2025, 1, 1))
result = manager.sweep_expired_credits()                                 # sweep
report = manager.sweep_expired_credits(dry_run=True)                     # preview only
```

### Team / shared balances

```python
team = store.create_team("Engineering", initial_balance=5000)
store.add_team_member(team.team_id, "user_abc", role="admin", spend_cap=1000)
result = manager.deduct_team(team.team_id, "user_abc", UsageMetrics(model="gpt-4", input_tokens=500))
```

### Spend caps

```python
from ducto.interface.models import SpendCap
store.set_spend_cap(SpendCap(user_id="user_abc", cap_type="daily", limit=100, action="deny"))
```

### Usage analytics

```python
from datetime import datetime, timedelta
now = datetime.now()
rows = manager.spend_by_user(now - timedelta(days=30), now)             # per-user totals
rows = manager.spend_by_model(now - timedelta(days=30), now)             # per-model spend
rows = manager.top_users(10, now - timedelta(days=30), now)              # top 10 users
rows = manager.daily_spend(now - timedelta(days=30), now)                # daily buckets
stats = manager.aggregate_stats(now - timedelta(days=30), now)           # aggregate summary
```

### Events

```python
from ducto.events import CreditEventEmitter
emitter = CreditEventEmitter()
manager = CreditManager(store=store, emitter=emitter)
emitter.on("credits.deducted", lambda e: print(f"User {e.user_id} spent credits"))
emitter.on("credits.low_balance", lambda e: send_alert(e.user_id, e.data["balance"]))
```

### Expression syntax

| Feature | Example |
|---------|---------|
| Arithmetic | `+`, `-`, `*`, `/`, `//`, `%`, `**` |
| Comparisons | `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in` |
| Boolean | `and`, `or`, `not` |
| Ternary | `X if cond else Y` |
| Functions | `ceil`, `floor`, `round`, `min`, `max`, `if(cond,t,f)`, `tier(v,t1,r1,t2,r2,...)`, `clamp(x,lo,hi)`, `percentile(p,v1,v2,...)` |

### Available metrics

| Variable | Source |
|----------|--------|
| `input_tokens` | `UsageMetrics.input_tokens` |
| `output_tokens` | `UsageMetrics.output_tokens` |
| `cache_read_tokens` | `UsageMetrics.cache_read_tokens` |
| `cache_write_tokens` | `UsageMetrics.cache_write_tokens` |
| `tool_calls` | `len(UsageMetrics.tool_calls)` |
| `search_queries` | `UsageMetrics.search_queries` |
| `search_results` | `UsageMetrics.search_results` |
| `web_search_calls` | `UsageMetrics.web_search_calls` |
| `code_exec_calls` | `UsageMetrics.code_exec_calls` |

## Storage Backends

| Store | Import | Deps | Use case |
|-------|--------|------|----------|
| `MemoryStore` | `ducto.interface.memory.MemoryStore` | None | Testing, dev |
| `HttpxSupabaseStore` | `ducto.interface.supabase.HttpxSupabaseStore` | `httpx` | Supabase production |
| `PostgresStore` | `ducto.interface.postgres.PostgresStore` | `psycopg2` | Direct PostgreSQL |

### Custom stores

Implement `ducto.interface.base.CreditStore` (ABC with 18 abstract methods).

## Credit Lifecycle

`CreditManager.deduct()` orchestrates:

1. **Calculate** — `PricingEngine.calculate(metrics)` → cost
2. **Plan allowance** — consume free allowance if user has a plan
3. **Spend cap check** — deny/warn/notify if configured limit exceeded
4. **Reserve** — `store.reserve_credits()` locks credits (auto-expires 10 min)
5. **Deduct** — `store.deduct_credits()` atomic deduction (idempotent)

### Additional operations

- **Refund:** `manager.refund_credits(tx_id, amount?)` — full or partial
- **Expire:** `manager.sweep_expired_credits(dry_run=True)` — preview or execute
- **Team deduct:** `manager.deduct_team(team_id, user_id, metrics)` — team pool
- **Analytics:** `spend_by_user`, `spend_by_model`, `top_users`, `daily_spend`, `aggregate_stats`
- **Events:** Subscribe via `CreditEventEmitter` for lifecycle hooks

## SQL Migrations

10 bundled migrations (`ducto migrate <url>`):

| File | Contents |
|------|----------|
| `001_credit_tables.sql` | Core tables + RLS |
| `002_credit_rpcs.sql` | Balance RPCs |
| `003_pricing_config.sql` | Config table + RPCs |
| `004_user_plans.sql` | Plans + usage windows |
| `005_credit_refunds.sql` | Refund RPC |
| `006_credit_expiry.sql` | Expiry sweep RPC |
| `007_usage_analytics.sql` | Analytics RPCs |
| `008_team_balances.sql` | Teams + members |
| `009_spend_caps.sql` | Spend cap RPC |
| `010_aggregate_stats.sql` | Aggregate stats RPC |

## Architecture

```
ducto/
  expr.py              # Safe AST expression evaluator
  config.py            # PricingConfig loading + validation
  engine.py            # PricingEngine — calculate, calculateBatch
  metrics.py           # UsageMetrics, ToolCall
  breakdown.py         # CostBreakdown
  events.py            # CreditEventEmitter pub/sub
  manager.py           # CreditManager orchestration
  interface/
    base.py            # CreditStore ABC (18 methods)
    models.py          # Pydantic schemas
    memory.py          # MemoryStore
    supabase.py        # HttpxSupabaseStore + run_migrations()
    postgres.py        # PostgresStore
  sql/                 # 010_*.sql
```

## Expression Safety

1. Parse `ast.parse(expr, mode="eval")`
2. Walk AST — each node type in an allowlist
3. Allowed functions: `ceil`, `floor`, `round`, `min`, `max`, `if`, `tier`, `clamp`, `percentile`
4. Rejects: attributes, subscripts, lambdas, comprehensions, imports
5. `__builtins__` emptied at evaluation time
6. All expressions validated at config load time

## Development

```bash
pip install "ducto[test]"
pytest
ruff check .
ruff format .
pyright
```

See [CONTRIBUTING.md](CONTRIBUTING.md).
