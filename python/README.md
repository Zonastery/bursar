# bursar

[![CI](https://github.com/Zonastery/bursar/actions/workflows/ci.yml/badge.svg)](https://github.com/Zonastery/bursar/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

Add usage-based credits to your AI SaaS in minutes — not weeks.

bursar is a drop-in credit calculation engine. Define pricing as math expressions
(per-model, per-tool, search/RAG, cache, fixed jobs), connect a database, and
start deducting credits. No billing infrastructure to build. Pricing lives in
your DB — update it live without redeploys.

```python
from bursar import CreditManager, UsageMetrics
from bursar.interface.supabase import HttpxSupabaseStore

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
pip install bursar

# With Supabase store
pip install "bursar[supabase]"

# With PostgreSQL store
pip install "bursar[postgres]"

# Development & testing
pip install "bursar[test]"
```

Requires Python 3.11+.

## Full docs

**[zonastery.github.io/bursar](https://zonastery.github.io/bursar/)** — Python API reference, expressions, configuration, examples.

## Quick Start

### 0. Stateless calculation (no database)

```python
from bursar import PricingEngine, UsageMetrics

engine = PricingEngine.from_dict({
    "version": 1,
    "models": {"_default": "input_tokens * 0.001 + output_tokens * 0.003"},
})

result = engine.calculate(UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
print(f"Total credits: {result.total}")
```

### 1. Install and migrate

```bash
pip install "bursar[postgres]"

# The connection string is read from DATABASE_URL (recommended) — keeping the
# password out of your shell history, `ps` output and CI logs.
export DATABASE_URL="postgresql://user:pass@host:5432/db"
bursar migrate
```

> A positional URL (`bursar migrate "postgresql://…"`) still works for convenience
> but is discouraged and prints a warning, since it leaks the password via the
> process list, shell history and CI logs.

Creates all tables (`user_credits`, `credit_transactions`, `credit_reservations`,
`credit_plans`, `credit_usage_window`, `credit_teams`, `credit_team_members`,
`credit_spend_caps`, `bursar_config`) and 20+ RPCs — all idempotent.

### 2. Pricing version management

```bash
# Apply new pricing (creates v1)
bursar pricing set - <<'JSON'
{
  "version": 1,
  "models": { "_default": "input_tokens * 0.01 + output_tokens * 0.03" },
  "plans": {
    "free": { "id": "free", "name": "Free Tier", "allowance": { "amount": 50000 } },
    "pro": { "id": "pro", "name": "Pro", "allowance": { "amount": 500000 } }
  }
}
JSON

# Apply with a label
bursar pricing set pricing.yaml --label "deploy-42"

# List all versions  (* = active)
bursar pricing list

# Switch active pricing
bursar pricing activate 1

# Diff two versions
bursar pricing diff 1 2

# Export a version as JSON
bursar pricing export 2

# Validate without applying
bursar pricing validate pricing.yaml
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
from bursar import CreditManager, UsageMetrics
from bursar.interface.postgres import PostgresStore

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
  "safety": { "overdraft_floor": 5 }
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
      "allowance": { "amount": 50000, "period": "calendar_month" },
      "rate_overrides": { "_default": "input_tokens * 0.02 + output_tokens * 0.06" },
      "entitlements": {
        "max_concurrency": { "value": 1 },
        "background_removal": { "value": true, "max_calls": 5, "period": "monthly", "on_exceed": "deny" }
      }
    },
    "pro": {
      "id": "pro",
      "name": "Pro Plan",
      "allowance": { "amount": 500000 }
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
from bursar.interface.models import SpendCap
store.set_spend_cap(SpendCap(user_id="user_abc", cap_type="daily", limit=100, action="deny"))
```

### Financial safety (leases)

Because bursar charges *after* the AI call, the safe pattern is an atomic **lease** taken *before* the work: `reserve` a worst-case hold against `available = balance − Σ(active holds)`, do the work, then `settle` the **actual** cost (de-clamped) or `release` to cancel. `reserve` is the only admission gate. Two presets: `strict_prepaid` (default; floor ≥ 0, structurally zero debt) and `overdraft` (negative floor; bills full actual; for paid users with auto-reload).

```python
from decimal import Decimal

manager = CreditManager(store=store, policy="strict_prepaid")  # or policy="overdraft", overdraft_floor=Decimal("-50")
lease = manager.reserve("user_abc", Decimal("40"))                     # worst-case hold
deduction = manager.settle("user_abc", lease.lease_id, Decimal("11"))  # actual cost; de-clamped
# on failure: manager.release("user_abc", lease.lease_id)              # idempotent
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
from bursar.events import CreditEventEmitter
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
| `HttpxSupabaseStore` | `bursar.interface.supabase.HttpxSupabaseStore` | `httpx` | Supabase production |
| `PostgresStore` | `bursar.interface.postgres.PostgresStore` | `psycopg2` | Direct PostgreSQL |

### Custom stores

Implement `bursar.interface.base.CreditStore` (ABC with 29 abstract methods).

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

10 bundled migrations (`DATABASE_URL=… bursar migrate`):

| File | Contents |
|------|----------|
| `001_core_schema.sql` | Core tables (`user_credits`, `credit_transactions`, `credit_reservations`) + RLS + signup bonus trigger |
| `002_credit_rpcs.sql` | `credits_add`, `get_credits_balance` |
| `003_bursar_config.sql` | Pricing config table + get/set/list/activate RPCs |
| `004_plans.sql` | Subscription plans, usage windows, allowance RPCs |
| `005_spend_caps.sql` | Spend cap table + `check_spend_cap` RPC |
| `006_refunds_and_expiry.sql` | `refund_credits`, `expire_credits` |
| `007_analytics.sql` | Analytics + transaction-listing RPCs |
| `008_teams.sql` | Team balance pools + RPCs |
| `009_deduct_and_leases.sql` | Atomic `deduct_with_allowance` + full lease lifecycle |
| `010_credit_tiers.sql` | Configurable credit tiers (priority-ordered balance buckets) |

## Architecture

```
bursar/
  expr.py              # Safe AST expression evaluator
  config.py            # BursarConfig loading + validation
  engine.py            # PricingEngine — calculate, calculateBatch
  metrics.py           # UsageMetrics, ToolCall
  breakdown.py         # CostBreakdown
  events.py            # CreditEventEmitter pub/sub
  credits_service.py   # Credit capability owned by the Bursar facade
  interface/
    base.py            # CreditStore ABC (29 abstract methods)
    models.py          # Pydantic schemas
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
pip install "bursar[test]"
pytest
ruff check .
ruff format .
pyright
```

See [CONTRIBUTING.md](CONTRIBUTING.md).
