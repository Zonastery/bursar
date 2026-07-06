# bursar JavaScript SDK

Credit billing engine for AI SaaS — TypeScript-first mirror of the Python SDK. Same public API surface, same money semantics, same lease lifecycle; all async, all `Decimal` (decimal.js).

## Stack
TypeScript (strict), `decimal.js` for all money (no native `number` for amounts), Vitest for tests. Three store backends: `MemoryStore` (testing), `PostgresStore` (`pg`), `HttpxSupabaseStore` (native fetch, zero extra deps). Exports: `@zonastery/bursar` (main), `@zonastery/bursar/node` (Node-only: `MemoryStore`, `PostgresStore`, `loadPricingFile`).

## Key source files

| File | Purpose |
|------|---------|
| `src/manager.ts` | `CreditManager` — full public API, all methods `async`. |
| `src/stores/credit-store.ts` | `CreditStore` abstract class — interface every store implements. |
| `src/stores/memory-store.ts` | `MemoryStore` — reference implementation; parity baseline. |
| `src/stores/postgres-store.ts` | `PostgresStore` — calls same SQL RPCs as Python via `pg`. |
| `src/stores/supabase-store.ts` | `HttpxSupabaseStore` — Supabase REST + service role key. |
| `src/types.ts` | All exported types: result types, `PlanDefinition`, `OperationPolicy`, `LeaseResult`, etc. |
| `src/engine.ts` | `PricingEngine` — expression evaluation, same logic as Python. |
| `src/errors.ts` | All error classes: `InsufficientCreditsError`, `ConcurrencyLimitError`, `FeatureNotEntitledError`, `LeaseExpiredError`, `LeaseNotFoundError`, etc. |
| `src/events.ts` (re-exported from `stores/`) | `CreditEventEmitter`, `CreditEvent`, 14 event types. |
| `src/metrics.ts` | `UsageMetrics`, `ToolCall`. |
| `src/index.ts` | Package exports — everything users `import from "@zonastery/bursar"`. |
| `src/node.ts` | Node-only subpath exports. |

## Architecture

```
CreditManager
  ├── PricingEngine              (calculate cost from UsageMetrics)
  ├── CreditStore                (abstract — memory / postgres / supabase)
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
- Parity with Python: same config, same rounding (`ROUND_HALF_UP`, 4dp), same result.

## Constructor signature
```typescript
new CreditManager(store, engine?, emitter?, options?)
// options: { policy?, overdraftFloor?, maxConcurrent?,
//            lowBalanceThreshold?, lowBalanceThresholds?,
//            onLowBalance?, defaultTtlSeconds? }
```

## Tests

| File | What it covers |
|------|----------------|
| `tests/memory-store.test.ts` | MemoryStore unit tests |
| `tests/credit-manager.test.ts` | CreditManager happy-path |
| `tests/lease.test.ts` | Lease lifecycle (27 tests, mirrors Python) |
| `tests/lease-adversarial.test.ts` | Concurrency invariants (30 tests) |
| `tests/tiers.test.ts` | Credit tiers — happy-path priority walk, refund LIFO, expiry, overdraft sink |
| `tests/tiers-adversarial.test.ts` | Credit tiers — concurrency, idempotent replay, config drift |
| `tests/postgres-store.test.ts` | `PostgresStore` unit tests against a mocked `pg.Pool` (no real DB) |
| `tests/store-integration.test.ts` | Real Postgres tests incl. `CreditManager` end-to-end tier coverage |
| `tests/security-rls.test.ts` | RLS/privilege lockdown against real Postgres roles (`anon`/`authenticated`/`service_role`) — the REVOKE/RLS checks `store-integration.test.ts` bypasses by connecting as a superuser |
| `tests/invariants.property.test.ts` | fast-check model-based property test — ledger conservation across grant/deduct/lease/refund sequences |
| `tests/engine.test.ts` | PricingEngine expression evaluation |

Run: `npm test`. Real-Postgres tests resolve a DSN from `DATABASE_URL` (CI's own
service container) or, failing that, a testcontainers-managed `postgres:16`
(Docker permitting) started automatically in `tests/global-setup.ts` — so a
bare `npm test` with Docker available exercises them too, not just CI.
Typecheck: `npm run typecheck`.
Lint: `npm run lint`.

## Parity rule
Behavior must match the Python SDK exactly. `MemoryStore` is the reference. JS Sets compare by reference — use `.toString()` keys when storing `Decimal` values in `Set`/`Map`. `Promise.all` in tests is sequential (single-threaded); concurrency tests assert invariants rather than race outcomes.
