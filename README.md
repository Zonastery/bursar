# ducto — Declarative Credit Calculation Engine

[![CI](https://github.com/apoorwv/ducto/actions/workflows/ci.yml/badge.svg)](https://github.com/apoorwv/ducto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![npm](https://img.shields.io/npm/v/@apoorwv/ducto)](https://www.npmjs.com/package/@apoorwv/ducto)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Add usage-based credits to your AI SaaS in minutes — not weeks.

```python
from ducto import CreditManager, UsageMetrics
from ducto.interface.supabase import HttpxSupabaseStore

store = HttpxSupabaseStore(url=supabase_url, key=service_role_key)
manager = CreditManager(store=store)
manager.load_pricing_from_store()

from decimal import Decimal

manager.add_credits("user_abc", Decimal("1000"))
# deduct() calculates the cost (a fractional Decimal — sub-credit costs are NOT
# truncated) and charges it atomically in a single server-side transaction.
manager.deduct("user_abc", UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
```

```typescript
import { CreditManager, MemoryStore } from "@apoorwv/ducto";

const store = new MemoryStore();
const manager = new CreditManager(store);
manager.publishPricingFromDict({
  version: 1,
  models: { "_default": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)" },
  plans: {
    free: { id: "free", name: "Free Tier", freeAllowance: 50000 },
    pro: { id: "pro", name: "Pro Plan", freeAllowance: 500000 },
  },
});

// Amounts accept a number or a decimal.js Decimal; money is tracked as Decimal
// internally (no float drift) and sub-credit costs are charged in full.
await manager.addCredits("user_abc", 1000);
// deduct() calculates the fractional cost and charges it atomically.
await manager.deduct("user_abc", { model: "gpt-4", inputTokens: 500, outputTokens: 200 });
```

## Features

- **Safe expression engine** — `min`, `max`, `if`, `tier`, `clamp`, `percentile`, `ceil`, `floor`, `round`. No eval/exec.
- **Database-backed pricing** — Live updates without redeploys. Dict loading for testing.
- **Multi-dimensional** — Per-model, per-tool, search/RAG, cache discounts, fixed-cost jobs.
- **Subscription plans** — Free monthly allowances consumed before balance deductions.
- **Refunds** — Full and partial credit reversals with duplicate detection.
- **Credit expiry** — Time-bound credits with low-water-mark sweep and dry-run mode.
- **Team / shared balances** — Separate team credit pools with per-member spend caps.
- **Spend caps** — Per-user daily/monthly limits with deny/warn/notify actions.
- **Usage analytics** — Time-windowed aggregation by user, model, daily, top users, and aggregate stats.
- **Event hooks** — Typed pub/sub for `credits.deducted`, `credits.low_balance`, `credits.expired`, and more.
- **Pluggable storage** — Supabase (zero HTTP deps), raw PostgreSQL, or in-memory.
- **Safe defaults** — `min_balance` floor, idempotent deductions, concurrent reservation protection.

## Documentation

Full docs at **[apoorwv.github.io/ducto](https://apoorwv.github.io/ducto/)**.

| Language | Package | Path |
|----------|---------|------|
| Python | `ducto` (PyPI) | [`python/`](/python) — [README](python/README.md) |
| TypeScript | `@apoorwv/ducto` (npm) | [`javascript/`](/javascript) — [README](javascript/README.md) |

## Quick Start

```bash
pip install ducto           # Python
npm install @apoorwv/ducto  # TypeScript
```

> **CLI availability:** The `ducto` CLI commands below are Python-only (`pip install ducto`). The JavaScript SDK provides equivalent library APIs without a CLI.

### 1. Migrate database

```bash
ducto migrate "postgresql://user:pass@host:5432/db"
```

Applies 15 SQL migrations — tables and RPCs for credits (including the atomic
`deduct_with_allowance` deduction), pricing versions, plans, refunds, expiry,
analytics, teams, spend caps, feature entitlements, transaction/usage listing,
and `NUMERIC` (fractional) money columns.

### 2. Pricing version management

Each `ducto pricing set` creates a new immutable version. Roll back anytime with `pricing activate`.

```bash
# Apply new pricing (creates v1)
ducto pricing set pricing.json

# Apply with a label
ducto pricing set pricing.json --label "deploy-42"

# List all versions  (* = active)
ducto pricing list

# Switch active pricing
ducto pricing activate 1

# Diff two versions
ducto pricing diff 1 2

# Export a version as JSON
ducto pricing export 2

# Validate without applying
ducto pricing validate pricing.json
```

### 3. Deduct credits

```python
from decimal import Decimal

manager.add_credits("user_abc", Decimal("5000"))
result = manager.deduct("user_abc", UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
# amount / balance_after are Decimal; allowance_consumed reports any free
# allowance applied. The whole deduction is one atomic, idempotency-keyable RPC.
print(f"Deducted {abs(result.amount)} credits. Balance: {result.balance_after}")
```

### Calculation only (no database)

```python
from ducto import PricingEngine, UsageMetrics
engine = PricingEngine.from_dict({"models": {"_default": "input_tokens * 0.001"}})
cost = engine.calculate(UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
```

## Feature Examples

### Refunds

```python
tx = manager.deduct("user_abc", UsageMetrics(model="gpt-4", input_tokens=500))
refund = manager.refund_credits(tx.transaction_id)                    # full refund
partial = manager.refund_credits(tx.transaction_id, amount=5)         # partial
```

```typescript
const tx = await manager.deduct("user_abc", { model: "gpt-4", inputTokens: 500 });
const refund = await manager.refundCredits(tx.transactionId);           // full refund
const partial = await manager.refundCredits(tx.transactionId, 5);       // partial
```

### Credit expiry

```python
manager.add_credits("user_abc", 100, "purchase", expires_at=datetime(2025, 1, 1))
result = manager.sweep_expired_credits()                               # sweep
report = manager.sweep_expired_credits(dry_run=True)                   # preview only
```

```typescript
await manager.addCredits("user_abc", 100, "purchase", null, new Date("2025-01-01"));
const result = await manager.sweepExpiredCredits();                      // sweep
const report = await manager.sweepExpiredCredits(true);                  // preview only
```

### Team / shared balances

```python
team = store.create_team("Engineering", initial_balance=5000)
store.add_team_member(team.team_id, "user_abc", role="admin", spend_cap=1000)
result = manager.deduct_team(team.team_id, "user_abc", UsageMetrics(model="gpt-4", input_tokens=500))
```

```typescript
const team = await store.createTeam("Engineering", 5000);
await store.addTeamMember(team.teamId, "user_abc", "admin", 1000);
const result = await manager.deductTeam(team.teamId, "user_abc", { model: "gpt-4", inputTokens: 500 });
```

### Spend caps

```python
from ducto.interface.models import SpendCap
store.set_spend_cap(SpendCap(user_id="user_abc", cap_type="daily", limit=100, action="deny"))
```

```typescript
store.setSpendCap({ userId: "user_abc", type: "daily", limit: 100, action: "deny" });
```

### Usage analytics

```python
from datetime import datetime, timedelta
now = datetime.now()
rows = manager.spend_by_user(now - timedelta(days=30), now)           # per-user totals
rows = manager.spend_by_model(now - timedelta(days=30), now)           # per-model spend
rows = manager.top_users(10, now - timedelta(days=30), now)            # top 10 users
rows = manager.daily_spend(now - timedelta(days=30), now)              # daily buckets
stats = manager.aggregate_stats(now - timedelta(days=30), now)         # aggregate summary
```

```typescript
const now = new Date();
const start = new Date(now.getTime() - 30 * 86400000);
await manager.spendByUser(start, now);                                  // per-user totals
await manager.spendByModel(start, now);                                  // per-model spend
await manager.topUsers(10, start, now);                                  // top 10 users
await manager.dailySpend(start, now);                                    // daily buckets
await manager.aggregateStats(start, now);                                // aggregate summary
```

### Events

```python
from ducto.events import CreditEvent, CreditEventEmitter
emitter = CreditEventEmitter()
manager = CreditManager(store=store, emitter=emitter)
emitter.on("credits.deducted", lambda e: print(f"User {e.user_id} spent credits"))
emitter.on("credits.low_balance", lambda e: send_alert(e.user_id, e.data["balance"]))
```

```typescript
import { CreditEventEmitter } from "@apoorwv/ducto";
const emitter = new CreditEventEmitter();
const manager = new CreditManager(store, null, emitter);
emitter.on("credits.deducted", (e) => console.log(`User ${e.userId} spent credits`));
emitter.on("credits.low_balance", (e) => sendAlert(e.userId, e.data?.balance));
```
