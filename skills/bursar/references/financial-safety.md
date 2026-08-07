# Bursar — Financial Safety

Bursar enforces financial invariants at the canonical account boundary.
This file is the invariants and anti-patterns an integration MUST respect
so it never corrupts money state. If your integration doesn't fit one of
these rules, change the integration — not the rules.

## The invariants

1. **Exact money.** Every amount is an exact `Decimal`; the schema stores
   `numeric(20,6)` quantized to 6 dp with `ROUND_HALF_UP` rounding. A
   charge like `800 / 1,000,000 × 0.0025` is never computed in floating
   point. _Why:_ float rounding silently creates or destroys credits at
   scale; ledger sums must reconcile to the exact cent/credit.
2. **One locked balance per account.** `credit_accounts.balance` is written
   only by the ledger-posting path, guarded by a `version` that every
   mutation checks. The hot path never re-derives the balance from history.
3. **Balance equals the ledger.** Every mutation appends a
   `credit_ledger_entries` row whose `balance_after` equals the account
   balance plus the entry's amount. A `check_ledger_balance` trigger
   enforces this at the database level and also verifies that a
   `reference_entry_id` belongs to the same account. _Why:_ two reads (the
   balance and the history) must never disagree.
4. **The ledger is append-only and idempotent.** Entries are never updated
   or deleted; `(account_id, idempotency_key)` is unique, so a retried
   write replays the original entry instead of double-posting.
5. **Available lot amounts equal derived bucket totals.** Buckets and lots
   must agree; debits consume lots in priority order via
   `credit_lot_allocations`.
6. **Strict-prepaid floors.** A strict-prepaid operation cannot cross its
   minimum balance; the floor is checked at admission _and_ at settlement.
7. **Leases settle at most once.** The lease status machine moves
   `settling → settled`; a settled or released lease cannot be charged
   again.
8. **Reversals are entries, not rewrites.** Refunds and expiry are
   `refund`/`expiry` ledger entries (with `refund_clawback` reversing the
   original debits) — never silent write-offs or projection rewrites.

## Hard do-nots

- **No float arithmetic on money.** Use `Decimal` (Python) / `decimal.js`
  `Decimal` (TS). The TS SDK amounts are `Decimal` values; SDKs accept
  numbers only as a convenience and coerce them.
- **No direct SQL writes to ledger tables.** Bursar owns the schema,
  migrations, and tenant lifecycle. Host migrations must not create,
  alter, or seed Bursar tables; do not insert into `bursar.tenants`.
- **No bypassing idempotency keys.** Every monetary mutation accepts an
  idempotency key. Omitting one generates a random key (safe but
  un-replayable); webhooks and workers must always pass a stable key.
- **No charging without admission/entitlement checks.** `deduct` is atomic
  with quota, feature, and operation checks; `reserve` is the only
  authoritative admission gate. `can_afford` / `canAfford` is advisory
  (UI only, non-locking, may be stale) — never use it as a gate.
- **No tenant-id mixing.** Never set a session-scoped tenant value on a
  pooled connection — it leaks into the next caller. Binding is
  transaction-local, per RPC.
- **No application-level balance counters.** There is no
  read-modify-write in application code; a counter that could drift from
  the ledger is forbidden.

## Idempotency

The store indexes `(account, idempotency_key)` uniquely — keys are scoped
**per account**. A retry with the same key and the same request replays the
original entry: the result comes back with `idempotent=True` / `idempotent`
and no second ledger row exists. Reusing a key with a **different** request
is a conflict, not a silent pass — two different payloads cannot share one
ledger row.

```python
first = bursar.credits.add_credits(
    user_id, 100, entry_type="purchase", idempotency_key="checkout:42",
)
replay = bursar.credits.add_credits(
    user_id, 100, entry_type="purchase", idempotency_key="checkout:42",
)
assert first.entry_id == replay.entry_id and replay.idempotent
```

```ts
const first = await bursar.credits.addCredits(userId, 100, {
  type: "purchase",
  idempotencyKey: "checkout:42",
});
const replay = await bursar.credits.addCredits(userId, 100, {
  type: "purchase",
  idempotencyKey: "checkout:42",
});
console.assert(first.entryId === replay.entryId && replay.idempotent);
```

Use provider event ids as keys in webhook handlers (`"checkout:cs_123456"`)
so redelivery is a no-op — never a double charge. Retries, worker
redeliveries, and `retry_bursar_operation` are safe for the same reason.
The account row lock makes the account single-writer: every mutation locks
the account before snapshotting the balance and checking idempotency, so
concurrent deductions cannot both read the same balance.

## Leases

Reserve before work, settle or release after:

- `reserve` captures a worst-case hold, the policy snapshot, and
  `minimum_balance`, and atomically enforces entitlement, quota,
  allowance, and `max_in_flight` — the only admission gate.
- Do the work; call `renew` before the TTL elapses on long jobs.
- `settle` bills the actual cost and finalizes; `release` returns an
  unused hold on failure.
- `run_billed` / `runBilled` wraps the three steps; on a `do_work`
  exception the lease is released automatically, and settlement is
  retried with a bounded attempt count because a failed settle's outcome
  may be unknown.

On a crash, the lease expires: the TTL plus the store's `expire_leases`
reaper reclaims the hold without ever charging it. Settlement honors the
policy captured at admission even if the user changes plans mid-flight, so
a plan change can neither strand approved work nor let it bypass the policy
it was admitted under.

## Multitenancy

One PostgreSQL schema with shared tables; every catalog, credit, usage,
quota, team, and billing row carries a mandatory `tenant_id`.

- Tenant-prefixed unique constraints let different tenants reuse subject,
  provider, and idempotency identifiers.
- Composite foreign keys prevent a row from referencing another tenant's
  rows even if privileged code has a bug.
- Business tables use **forced row-level security**, so isolation remains
  active even when the application connects as PostgreSQL
  `service_role`, which normally bypasses RLS. Tenant RPCs are
  `SECURITY DEFINER` functions owned by the `bursar_runtime` role
  (`NOLOGIN`, `NOBYPASSRLS`).
- Each SDK checks out one pooled connection, starts a
  transaction, sets `bursar.tenant_id` with transaction-local scope,
  performs the RPC, then commits/rolls back before releasing the
  connection. Missing tenant context fails closed on writes; suspended and
  closed tenants cannot read or mutate business rows.
- A trusted PostgREST path may source the tenant from
  `request.jwt.claims.app_metadata.tenant_id`. Bursar never reads
  `user_metadata` — an end user can change it.
- Host triggers delegate to `bursar.provision_subject_account_on_insert('<tenant-slug>')`
  and must not read Bursar tables or implement plan assignment themselves.
- Outbox claims are tenant-filtered (one runtime can't take another
  tenant's work); exported payloads and S3 keys
  (`<prefix>/tenants/<tenant-id>/billing-events/...`, prefix defaults to
  `bursar`) always carry `tenant_id`. Keep this field in any custom
  outbox handler or projection.

## Error handling

Every failure is a `BursarError` subclass with a stable `code`, a
`category`, and a `retryable` flag. Categories: `invalid_request`,
`payment_required`, `forbidden`, `not_found`, `conflict`, `rate_limited`,
`unavailable`, `internal`.

| Error                                                                          | Code                                  | Category                    | Retryable |
| ------------------------------------------------------------------------------ | ------------------------------------- | --------------------------- | --------- |
| `InsufficientCreditsError`                                                     | `INSUFFICIENT_CREDITS`                | payment_required (HTTP 402) | no        |
| `QuotaExceededError`                                                           | `QUOTA_EXCEEDED`                      | rate_limited                | no        |
| `ConcurrencyLimitError`                                                        | `CONCURRENCY_LIMIT_REACHED`           | rate_limited                | no        |
| `CapReachedError`                                                              | `CAP_REACHED`                         | rate_limited                | no        |
| `FeatureNotEntitledError`                                                      | `FEATURE_NOT_ENTITLED`                | forbidden                   | no        |
| `OperationNotAllowedError`                                                     | `OPERATION_NOT_ALLOWED`               | forbidden                   | no        |
| `LeaseExpiredError`                                                            | `LEASE_EXPIRED`                       | conflict                    | no        |
| `LeaseNotFoundError`                                                           | `LEASE_NOT_FOUND`                     | not_found                   | no        |
| `RefundError`                                                                  | `REFUND_REJECTED`                     | conflict                    | no        |
| `PricingNotLoadedError`                                                        | `PRICING_NOT_LOADED`                  | unavailable                 | no        |
| `StoreUnavailableError` / `StoreTimeoutError`                                  | `STORE_UNAVAILABLE` / `STORE_TIMEOUT` | unavailable                 | **yes**   |
| `ConfigError`, `StoreError`, `StoreClosedError`, `CapabilityNotSupportedError` | …                                     | per class                   | per class |

Only SDK-classified transient failures (`retryable = True`, i.e.
`StoreUnavailableError`/`StoreTimeoutError`) should be retried. Retry with
`retry_bursar_operation` (Python) / `retryBursarOperation` (TS), which
retry only retryable failures with exponential backoff and jitter:

```python
from bursar.retry import retry_bursar_operation, BursarRetryOptions
from bursar.errors import is_retryable_bursar_error

retry_bursar_operation(operation, *args, retry_options=BursarRetryOptions(max_attempts=3))
```

```ts
import {
  retryBursarOperation,
  type BursarRetryOptions,
} from "@zonastery/bursar";

await retryBursarOperation(operation, ...args);
```

`BursarRetryOptions` defaults: `max_attempts=3`, `base_delay_seconds=0.25`,
`max_delay_seconds=2.0`, `factor=2.0`, `jitter=true`,
`max_elapsed_seconds=30.0`, plus optional `should_retry` and `on_retry`
callbacks. Helper predicates: `is_retryable_bursar_error` /
`isRetryableBursarError`. For HTTP projections, `bursar_error_http_status`
maps category to status (402 for `payment_required`, 429 for
`rate_limited`, 503 for `unavailable`, …); `to_dict` / `toJSON` give a
stable, safe serialization.

## Verification checklist

Run through this after writing integration code:

1. Every monetary write passes an explicit idempotency key (provider event
   id for webhooks); replaying the same call returns `idempotent=True` with
   the same `entry_id` and no second ledger row.
2. No `float`/`number` arithmetic anywhere in the money path — only
   `Decimal` (or `decimal.js` `Decimal` in TS).
3. The same idempotency key is never reused with a different payload
   (that must be a conflict, not a silent pass).
4. No direct SQL against Bursar-owned tables from host migrations or seed
   data — schema, migrations, and tenants are all Bursar-owned.
5. Long-running work goes through `reserve` → (renew) → `settle`/`release`
   or `run_billed`; `can_afford` is used only for advisory UI.
6. Every store is constructed with the correct `tenant_id`; nothing sets a
   session-scoped tenant on a pooled connection.
7. `deduct` is never called without the quota/feature/operation checks it
   performs atomically, and unmetered inputs are handled (reject or
   unmatched charge policy) rather than crashing.
8. Refunds go through `refund_credits(entry_id, amount=...)`, never by
   adjusting the balance by hand; over-refund raises `RefundError`.
9. Expiry runs as a scheduled `sweep_expired_credits` (or lazy expiry),
   never as a silent balance write-off.
10. Retry logic wraps only retryable failures
    (`StoreUnavailableError`/`StoreTimeoutError`); non-retryable errors
    surface immediately with their stable code.
11. After a test scenario, `get_balance` agrees with the summed
    `list_ledger_entries` — the balance is the sum of its entries, always.
