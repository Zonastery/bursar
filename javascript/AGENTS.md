# bursar JavaScript SDK

Credit billing engine for AI SaaS — TypeScript-first mirror of the Python SDK. Same public API surface, same money semantics, same lease lifecycle; all async, all `Decimal` (decimal.js).

## Stack
TypeScript (strict), `decimal.js` for all money (no native `number` for amounts), Vitest for tests, and Bun 1.3.14 for dependency management and script orchestration. Published output targets Node.js 22 or newer. One store backend: `PostgresStore` (`pg`). Exports: `@zonastery/bursar` (main), `@zonastery/bursar/node` (Node-only: `loadPricingFile`).

## Key source files

| File | Purpose |
|------|---------|
| `src/credits/service.ts` | Internal credit capability used by the `Bursar` facade. |
| `src/credits/store.ts` | `CreditStore` abstract class — interface every store implements. |
| `src/credits/postgres/store.ts` | `PostgresStore` — calls SQL RPCs via `pg`. |
| `src/credits/events.ts` | `CreditEventEmitter`, `CreditEvent`, event types. |
| `src/credits/types/` | All exported result types (`LeaseResult`, `BalanceResult`, ...). `PlanDefinition` lives in `config.ts`. |
| `src/engine.ts` | `PricingEngine` — expression evaluation, same logic as Python. |
| `src/errors.ts` | All error classes: `InsufficientCreditsError`, `ConcurrencyLimitError`, `FeatureNotEntitledError`, `LeaseExpiredError`, `LeaseNotFoundError`, etc. |
| `src/metrics.ts` | `UsageMetrics`, `ToolCall`. |
| `src/config.ts` | `loadConfigFromDict` — pricing config loading and validation. |
| `src/index.ts` | Package exports — everything users `import from "@zonastery/bursar"`. |
| `src/node.ts` | Node-only subpath exports. |

## Architecture

```
Bursar facade
  ├── PricingEngine              (calculate cost from UsageMetrics)
  ├── CreditStore                (abstract — PostgresStore is the only implementation)
  │     ├── deductWithAllowance()   atomic: allowance→cap→floor→debit
  │     ├── createLease / settleLease / releaseLease / renewLease
  │     └── ... (30+ abstract methods)
  └── CreditEventEmitter         (optional pub/sub)
```

**Hot path:** `manager.deduct()` → `store.deductWithAllowance()` (one atomic RPC).

**Safe path:** `manager.reserve()` → do work → `manager.settle()` or `manager.release()`. Use `manager.runBilled()` as a one-call shortcut.

**Financial-safety presets** (constructor `options.policy`):
- `strict_prepaid` (default) — floor ≥ 0, zero debt.
- `overdraft` — negative `overdraftFloor`, bills full actual at settle.

## Money invariants
- All amounts are `Decimal` from `decimal.js` — never plain `number`.
- Import: `import Decimal from "decimal.js"`.
- Money fields on result objects are always `Decimal`, not strings or numbers.
- Parity with Python: same config, same rounding (`ROUND_HALF_UP`, 6dp), same result.

## Constructor signature
```typescript
new Bursar({ creditStore, creditsOptions?, billingStore?, billingOptions? })
// options: { policy?, overdraftFloor?, maxConcurrent?,
//            lowBalance?, lowBalanceThresholds?,
//            onLowBalance?, defaultTtlSeconds? }
```

## Tests

| File | What it covers |
|------|----------------|
| `tests/billing-integration.test.ts` | Billing lifecycle against real Postgres |
| `tests/config-parity.test.ts` | Config loading parity with Python |
| `tests/config.test.ts` | Config validation edge cases |
| `tests/dodo-webhook-signature.test.ts` | Dodo webhook signature verification |
| `tests/engine.test.ts` | PricingEngine expression evaluation |
| `tests/events.test.ts` | CreditEventEmitter pub/sub |
| `tests/expr.test.ts` | Expression parser/evaluator edge cases |
| `tests/load-pricing-file.test.ts` | File loading for JSON/YAML |
| `tests/postgres-store.test.ts` | PostgresStore unit tests against a mocked `pg.Pool` (no real DB) |
| `tests/store-integration.test.ts` | Real Postgres tests incl. facade-owned credit capability end-to-end |
| `tests/security-rls.test.ts` | RLS/privilege lockdown against real Postgres roles |

Run: `bun run test`. Real-Postgres tests resolve a DSN from `DATABASE_URL` (CI's own
service container) or, failing that, a testcontainers-managed PostgreSQL 16 +
pg_partman 5 instance (Docker permitting) started automatically in
`tests/global-setup.ts` — so a bare `bun run test` with Docker available
exercises them too, not just CI.
Typecheck: `bun run typecheck`.
Lint: `bun run lint`.

## Parity rule
Behavior must match the Python SDK exactly. `PostgresStore` is the reference. JS Sets compare by reference — use `.toString()` keys when storing `Decimal` values in `Set`/`Map`. `Promise.all` in tests is sequential (single-threaded); concurrency tests assert invariants rather than race outcomes.
