# ducto

[![CI](https://github.com/apoorwv/ducto/actions/workflows/ci.yml/badge.svg)](https://github.com/apoorwv/ducto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

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
- **Pluggable storage** — `CreditStore` adapters: Supabase, PostgreSQL, in-memory.
- **Safe defaults** — `min_balance` floor, atomic idempotent deductions, fractional `Decimal` credits (no truncation).
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
    "version": 1,
    "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
})

result = engine.calculate(UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
print(f"Total credits: {result.total}")
```

### 1. Install and migrate

```bash
pip install "ducto[postgres]"

# The connection string is read from DATABASE_URL (recommended) — keeping the
# password out of your shell history, `ps` output and CI logs.
export DATABASE_URL="postgresql://user:pass@host:5432/db"
ducto migrate
```

> A positional URL (`ducto migrate "postgresql://…"`) still works for convenience
> but is discouraged and prints a warning, since it leaks the password via the
> process list, shell history and CI logs.

Creates all tables (`user_credits`, `credit_transactions`, `credit_reservations`,
`credit_plans`, `credit_usage_window`, `credit_teams`, `credit_team_members`,
`credit_spend_caps`, `credit_pricing_config`) and 20+ RPCs — all idempotent.

### 2. Pricing version management

```bash
# Apply new pricing (creates v1)
ducto pricing set - <<'JSON'
{
  "version": 1,
  "models": { "_default": "input_tokens * 0.01 + output_tokens * 0.03" },
  "plans": {
    "free": { "id": "free", "name": "Free Tier", "free_allowance": 50000 },
    "pro": { "id": "pro", "name": "Pro", "free_allowance": 500000 }
  }
}
JSON

# Apply with a label
ducto pricing set pricing.yaml --label "deploy-42"

# List all versions  (* = active)
ducto pricing list

# Switch active pricing
ducto pricing activate 1

# Diff two versions
ducto pricing diff 1 2

# Export a version as JSON
ducto pricing export 2

# Validate without applying
ducto pricing validate pricing.yaml
```

Each `pricing set` creates a new immutable version. Roll back with `pricing activate <version>`.

| Command | Description |
|---------|-------------|
| `pricing set <file> [--label <msg>]` | Apply config (always creates new version) |
| `pricing get` | Show active config |
| `pricing list` | List all versions |
| `pricing activate <version>` | Switch to any version |
| `pricing validate <file>` | Dry-run validate |
| `pricing diff <v1> <v2>` | Unified diff between versions |
| `pricing export <version>` | Dump version as JSON |

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
  "version": 1,
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
  "version": 1,
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
| Arithmetic | `+`, `-`, `*`, `/`, `//`, `%` (exponentiation `**` is rejected at validate time) |
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

Implement `ducto.interface.base.CreditStore` (ABC with 29 abstract methods).

## Credit Lifecycle

`CreditManager.deduct()`:

1. **Calculate** — `PricingEngine.calculate(metrics)` → `cost` (exact `Decimal`, no truncation)
2. **Short-circuit** — if `cost <= 0`, return a zero-amount result without touching the store
3. **Atomic charge** — one `store.deduct_with_allowance(...)` call applies plan allowance,
   spend-cap enforcement, the `min_balance` floor and the balance debit inside a **single
   transaction**, keyed by `idempotency_key` (a replay returns the original result)

The legacy two-phase `reserve_credits` + `deduct_credits` API is still available on the store
for callers that need a reservation step.

### Additional operations

- **Refund:** `manager.refund_credits(tx_id, amount?)` — full or partial
- **Expire:** `manager.sweep_expired_credits(dry_run=True)` — preview or execute
- **Team deduct:** `manager.deduct_team(team_id, user_id, metrics)` — team pool
- **Analytics:** `spend_by_user`, `spend_by_model`, `top_users`, `daily_spend`, `aggregate_stats`
- **Events:** Subscribe via `CreditEventEmitter` for lifecycle hooks

## SQL Migrations

15 bundled migrations (`DATABASE_URL=… ducto migrate`):

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
| `011_feature_entitlements.sql` | Feature entitlement RPC |
| `012_list_transactions.sql` | List user transactions RPC |
| `013_list_usage_events.sql` | List usage events RPC |
| `014_numeric_money.sql` | Convert money columns to `NUMERIC(18,4)` |
| `015_atomic_deduct.sql` | Atomic `deduct_with_allowance` RPC |

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
    base.py            # CreditStore ABC (29 abstract methods)
    models.py          # Pydantic schemas
    memory.py          # MemoryStore
    supabase.py        # HttpxSupabaseStore + run_migrations()
    postgres.py        # PostgresStore
  sql/                 # 001_*.sql … 015_*.sql (15 migrations)
```

## Expression Safety

1. Parse `ast.parse(expr, mode="eval")`
2. Walk AST — each node type in an allowlist
3. Allowed functions: `ceil`, `floor`, `round`, `min`, `max`, `if`, `tier`, `clamp`, `percentile`
4. Rejects: attributes, subscripts, lambdas, comprehensions, imports, exponentiation (`**`)
5. Division / modulo by zero and non-finite results raise `ExpressionError` (never `inf`/`NaN`)
6. `__builtins__` emptied at evaluation time
7. All expressions — and their variable names — validated at config load time

## Development

```bash
pip install "ducto[test]"
pytest
ruff check .
ruff format .
pyright
```

See [CONTRIBUTING.md](CONTRIBUTING.md).
