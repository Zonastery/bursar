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

manager.add_credits("user_abc", 1000)
manager.deduct("user_abc", UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
```

```typescript
import { CreditManager, MemoryStore } from "@apoorwv/ducto";

const store = new MemoryStore();
const manager = new CreditManager(store);
manager.publishPricingFromDict({
  version: 2,
  models: { "_default": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)" },
  plans: {
    free: { id: "free", name: "Free Tier", freeAllowance: 50000 },
    pro: { id: "pro", name: "Pro Plan", freeAllowance: 500000 },
  },
});

await manager.addCredits("user_abc", 1000);
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

### 1. Migrate database

```bash
ducto migrate "postgresql://user:pass@host:5432/db"
```

Creates 10 tables/RPC groups for credits, pricing, plans, refunds, expiry, analytics, teams, and spend caps.

### 2. Seed pricing

```bash
ducto pricing set pricing.json
```

### 3. Deduct credits

```python
manager.add_credits("user_abc", 5000)
result = manager.deduct("user_abc", UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
print(f"Deducted {abs(result.amount)} credits. Balance: {result.balance_after}")
```

### Calculation only (no database)

```python
from ducto import PricingEngine, UsageMetrics
engine = PricingEngine.from_dict({"version": 1, "models": {"_default": "input_tokens * 0.001"}})
cost = engine.calculate(UsageMetrics(model="gpt-4", input_tokens=500, output_tokens=200))
```
