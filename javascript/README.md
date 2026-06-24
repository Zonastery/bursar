# @apoorwv/ducto

[![CI](https://github.com/apoorwv/ducto/actions/workflows/ci.yml/badge.svg)](https://github.com/apoorwv/ducto/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/@apoorwv/ducto)](https://www.npmjs.com/package/@apoorwv/ducto)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/apoorwv/ducto/blob/main/LICENSE)

Add usage-based credits to your AI SaaS in minutes — not weeks.

ducto is a drop-in credit calculation engine. Define pricing as math expressions
(per-model, per-tool, search/RAG, cache, fixed jobs), connect a database, and
start deducting credits. Pricing lives in your DB — update it live without redeploys.

```typescript
import { CreditManager, MemoryStore } from "@apoorwv/ducto";

const store = new MemoryStore();
const manager = new CreditManager(store);

manager.publishPricingFromDict({
  models: { "_default": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)" },
  plans: {
    free: { id: "free", name: "Free Tier", freeAllowance: 50000 },
    pro: { id: "pro", name: "Pro Plan", freeAllowance: 500000 },
  },
});

await manager.addCredits("user_abc", 1000);
```

Works in Node.js 18+, Bun, and Deno.

## Features

- **Safe expression engine** — Recursive descent parser with strict allowlist. `min`, `max`, `if`, `tier`, `clamp`, `ceil`, `floor`, `round`, `percentile`. No eval/Function().
- **Plan-based pricing** — Subscription plans with free monthly allowances and rate overrides. Allowance consumed before balance.
- **Refunds** — Full and partial credit reversals with duplicate detection.
- **Credit expiry / TTL** — Time-bound credits with `expiresAt` on `addCredits`. Sweep with dry-run mode.
- **Team / shared balances** — Separate team credit pools with per-member spend caps.
- **Spend caps** — Per-user daily/monthly limits with `deny`, `warn`, `notify` actions. Per-model caps supported.
- **Usage analytics** — `spendByUser`, `spendByModel`, `topUsers`, `dailySpend`, `aggregateStats` across time windows.
- **Event hooks** — Typed pub/sub for `credits.deducted`, `credits.added`, `credits.refunded`, `credits.expired`, `credits.cap_reached`, `credits.cap_warning`, `credits.low_balance`.
- **Database-backed pricing** — Live updates without redeploys. Dict loading for testing.
- **Multi-dimensional** — Per-model (with `_default` fallback), per-tool overrides, search/RAG, cache discounts, fixed-cost jobs.
- **Pluggable storage** — Reserve-then-deduct via `CreditStore`: Supabase (native fetch, zero deps), PostgreSQL (`pg`), or in-memory.
- **Safe defaults** — `minBalance` floor, idempotent deductions, concurrent reservation protection.
- **Auditable** — Structured `CostBreakdown` with per-dimension costs.

## Installation

```bash
npm install @apoorwv/ducto

# PostgreSQL store (optional)
npm install pg

# YAML pricing loading (optional)
npm install js-yaml
```

Requires Node.js 18+ (native `fetch` for Supabase store).

## Full docs

**[apoorwv.github.io/ducto](https://apoorwv.github.io/ducto/)** — JS API reference, expressions, configuration, examples.

## Quick Start

### Calculation only (no database)

```typescript
import { PricingEngine } from "@apoorwv/ducto";

const engine = PricingEngine.fromDict({
  models: {
    "gpt-4": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)",
    "_default": "input_tokens * (0.001 / 1000) + output_tokens * (0.003 / 1000)",
  },
});

const cost = engine.calculate({
  model: "gpt-4",
  inputTokens: 500,
  outputTokens: 200,
});
console.log(`Total: ${cost.total}`); // 0.011
```

### Full credit lifecycle (in-memory)

```typescript
import { CreditManager, MemoryStore } from "@apoorwv/ducto";

const store = new MemoryStore();
const manager = new CreditManager(store);

manager.publishPricingFromDict({
  models: { "_default": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)" },
  plans: {
    free: { id: "free", name: "Free Tier", freeAllowance: 50000 },
    pro: { id: "pro", name: "Pro Plan", freeAllowance: 500000 },
  },
});

await manager.addCredits("user_abc", 1000);
const result = await manager.deduct(
  "user_abc",
  { model: "gpt-4", inputTokens: 500, outputTokens: 200 },
  "idempotency-key-123",
);
console.log(`Remaining balance: ${(await manager.getBalance("user_abc")).balance}`);
```

### Production with Supabase

```typescript
import { CreditManager } from "@apoorwv/ducto";
import { HttpxSupabaseStore } from "@apoorwv/ducto";

const store = new HttpxSupabaseStore(
  "https://your-project.supabase.co",
  "service_role_key"
);
const manager = new CreditManager(store);
await manager.loadPricingFromStore();
await manager.addCredits("user_abc", 5000);
```

### Plan-based pricing

```typescript
import { CreditManager, MemoryStore } from "@apoorwv/ducto";

const store = new MemoryStore();
const manager = new CreditManager(store);

manager.publishPricingFromDict({
  models: { "_default": "input_tokens * 1" },
  plans: {
    free: { id: "free", name: "Free Tier", freeAllowance: 50000 },
  },
});
await store.setUserPlan("user-1", "free");
await manager.addCredits("user-1", 10);

// First 50000 credits are free — no balance deduction
const result = await manager.deduct("user-1", { inputTokens: 5 });
console.log(result.amount); // 0 — covered by allowance
```

## Feature Examples

### Refunds

```typescript
const tx = await manager.deduct("user-1", { model: "gpt-4", inputTokens: 100 });
const refund = await manager.refundCredits(tx.transactionId);             // full refund
const partial = await manager.refundCredits(tx.transactionId, 5);         // partial
```

### Credit expiry

```typescript
await manager.addCredits("user-1", 100, "purchase", null, new Date("2025-01-01"));
const result = await manager.sweepExpiredCredits();                        // sweep
const report = await manager.sweepExpiredCredits(true);                    // preview only
```

### Team / shared balances

```typescript
const team = await store.createTeam("Engineering", 5000);
await store.addTeamMember(team.teamId, "user-1", "admin", 1000);
const result = await manager.deductTeam(team.teamId, "user-1", { model: "gpt-4", inputTokens: 500 });
```

### Spend caps

```typescript
store.setSpendCap({ userId: "user-1", type: "daily", limit: 100, action: "deny" });
```

### Usage analytics

```typescript
const now = new Date();
const start = new Date(now.getTime() - 30 * 86400000);
await manager.spendByUser(start, now);                                     // per-user totals
await manager.spendByModel(start, now);                                     // per-model spend
await manager.topUsers(10, start, now);                                     // top 10 users
await manager.dailySpend(start, now);                                       // daily buckets
await manager.aggregateStats(start, now);                                   // aggregate summary
```

### Events

```typescript
import { CreditEventEmitter } from "@apoorwv/ducto";
const emitter = new CreditEventEmitter();
const manager = new CreditManager(store, null, emitter);
emitter.on("credits.deducted", (e) => console.log(`User ${e.userId} spent credits`));
emitter.on("credits.low_balance", (e) => sendAlert(e.userId, e.data?.balance));
```

## Pricing Configuration

Basic example:

```json
{
  "models": {
    "gpt-4": "input_tokens * (0.01 / 1000) + output_tokens * (0.03 / 1000)",
    "_default": "input_tokens * (0.001 / 1000) + output_tokens * (0.003 / 1000)"
  },
  "tools": { "_default": "tool_calls * 5 / 1000" },
  "search": { "costs": "search_queries * 0.5 + search_results * 0.05" },
  "cache": { "discount": "-cache_read_tokens * (0.001 / 1000)" },
  "fixed": { "batch_train": 100 },
  "minBalance": 5
}
```

### Expression syntax

| Feature | Example |
|---------|---------|
| Arithmetic | `+`, `-`, `*`, `/`, `//`, `%`, `**` |
| Comparisons | `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in` |
| Boolean | `and`, `or`, `not` |
| Ternary | `X if cond else Y` |
| Functions | `ceil`, `floor`, `round`, `min`, `max`, `if(cond,t,f)`, `tier(v,t1,r1,t2,r2,...)`, `clamp(x,lo,hi)`, `percentile(p,v1,v2,...)` |

### Loading from file

```typescript
import { loadPricingFile, PricingEngine } from "@apoorwv/ducto";

const data = await loadPricingFile("./pricing.yaml");
const engine = PricingEngine.fromDict(data);
```

### Available metrics

| Variable | Source | Type |
|----------|--------|------|
| `input_tokens` | `metrics.inputTokens` | `number` |
| `output_tokens` | `metrics.outputTokens` | `number` |
| `cache_read_tokens` | `metrics.cacheReadTokens` | `number` |
| `cache_write_tokens` | `metrics.cacheWriteTokens` | `number` |
| `tool_calls` | `metrics.toolCalls.length` | `number` |
| `search_queries` | `metrics.searchQueries` | `number` |
| `search_results` | `metrics.searchResults` | `number` |
| `web_search_calls` | `metrics.webSearchCalls` | `number` |
| `code_exec_calls` | `metrics.codeExecCalls` | `number` |

## Store adapters

| Store | Import | Deps | Use case |
|-------|--------|------|----------|
| `MemoryStore` | `@apoorwv/ducto` | None | Testing, development |
| `HttpxSupabaseStore` | `@apoorwv/ducto` | Node 18+ (`fetch`) | Supabase production |
| `PostgresStore` | `@apoorwv/ducto` | `pg` | Direct PostgreSQL |

## API

### `PricingEngine`

```typescript
PricingEngine.fromDict(data): PricingEngine
engine.calculate(metrics: UsageMetrics): CostBreakdown
engine.calculateBatch(metrics: UsageMetrices[]): CostBreakdown[]
engine.resolveModel(modelVersion: string): string | null
engine.hasModel(modelName: string): boolean
engine.getFixedCost(jobName: string): number | null
engine.pricingSchema(): PricingConfigData
engine.minBalance: number
```

### `CreditManager`

```typescript
new CreditManager(store: CreditStore, engine?, emitter?)

// Pricing
manager.publishPricingFromDict(data): void
manager.loadPricingFromStore(): Promise<void>
manager.publishPricing(config, label?): void

// Balance ops
manager.getBalance(userId): Promise<BalanceResult>
manager.addCredits(userId, amount, type?, metadata?, expiresAt?): Promise<AddCreditsResult>
manager.reserveCredits(userId, amount, opType?, metadata?, minBalance?): Promise<ReserveResult>
manager.deduct(userId, metrics, idempotencyKey?, metadata?): Promise<DeductionResult>
manager.deductFixed(userId, jobName, idempotencyKey?, metadata?): Promise<DeductionResult>

// Refunds
manager.refundCredits(transactionId, amount?, reason?, metadata?): Promise<RefundResult>

// Expiry
manager.sweepExpiredCredits(dryRun?): Promise<SweepResult>

// Teams
manager.deductTeam(teamId, userId, metrics, metadata?): Promise<TeamDeductionResult>

// Analytics
manager.spendByUser(start, end): Promise<SpendByUserRow[]>
manager.spendByModel(start, end): Promise<SpendByModelRow[]>
manager.topUsers(limit, start, end): Promise<TopUserRow[]>
manager.dailySpend(start, end): Promise<DailySpendRow[]>
manager.aggregateStats(start, end): Promise<AggregateStats>

// Events
const emitter = new CreditEventEmitter();
emitter.on("credits.deducted", handler);
emitter.on("credits.low_balance", handler);
// ... credits.added, credits.refunded, credits.expired,
//     credits.cap_reached, credits.cap_warning

// Properties
manager.pricingEngine: PricingEngine | null
```

## License

MIT
