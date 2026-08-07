---
name: bursar
description: Integrate and review Bursar usage metering, prepaid credits, ledger accounting, plans, allowances, quotas, spend caps, leases, subscriptions, top-ups, auto-recharge, pricing configuration, and billing events in Python or TypeScript applications. Use when work mentions Bursar, @zonastery/bursar, the bursar CLI, PostgresStore, BursarConfig, credit or token metering, replay-safe billing, tenant-scoped ledgers, payment webhooks, or reserve-and-settle workflows.
---

# Integrate Bursar

Treat Bursar as the application's financial boundary. Route pricing, credit mutations, admission checks, and provider events through one tenant-bound `Bursar` facade.

## Establish the integration context

1. Read repository instructions before changing code.
2. Identify the installed Bursar version from `pyproject.toml`, a lockfile, or `package.json`.
3. Identify the language, web framework, database adapter, tenant source, and payment provider already in use.
4. Inspect existing Bursar construction, configuration, migrations, and error handling before adding a second path.
5. Read the smallest canonical source needed from the lookup table below.

Do not infer current call signatures from this skill. Confirm them in the installed package or generated API reference.

## Select the workflow

Use this decision table before writing code:

| Requirement | Use | Do not use |
| --- | --- | --- |
| Charge work with a known final measurement | `credits.deduct` | Manual price arithmetic followed by a flat debit |
| Admit work whose final cost is unknown | `credits.reserve`, then `settle` or `release`; prefer `run_billed` when its callback model fits | `can_afford` as an admission gate |
| Add purchased or granted value | `credits.add_credits` / `addCredits` with a stable idempotency key | Direct balance updates or ledger inserts |
| Reverse a charge | `credits.refund_credits` / `refundCredits` against the source entry | A compensating balance adjustment without a reference |
| Process a payment webhook | Verify with the provider adapter, normalize the event, then use the Bursar billing or commerce facade | Crediting from an unverified payload |
| Preview availability in a user interface | `can_afford` / `canAfford` | Treating the preview as authoritative |
| Change pricing or plans | Validate, publish, and activate a configuration revision | Scattered pricing constants or direct catalog writes |

## Preserve the financial invariants

Apply every rule below. Stop and explain the conflict if the surrounding application cannot satisfy one.

1. Use exact decimal values. Use `Decimal` in Python and the SDK's accepted decimal representation in TypeScript. Store exact configuration values as strings.
2. Pass a stable idempotency key to every replayable monetary mutation. Derive webhook keys from verified provider event identifiers and job keys from durable application identifiers.
3. Reuse an idempotency key only for the same account, operation, and payload. Treat a same-key different-payload result as a conflict.
4. Construct every store with the resolved `tenant_id`. Keep tenant context transaction-local and never set it at session scope on a shared pool.
5. Mutate balances only through the SDK. Do not add SQL writes, triggers, counters, or host migrations that alter Bursar-owned tables.
6. Use `deduct` as the atomic gate for measured immediate work. Use `reserve` as the atomic gate for uncertain-cost or long-running work.
7. Settle each lease once. Release it when work fails. Renew it before expiry when work continues beyond the original time-to-live.
8. Preserve the append-only ledger. Post refunds, expiry, revocation, purchases, and charges as linked accounting events.
9. Retry only errors the SDK marks retryable. Keep the original idempotency key across every retry.
10. Keep database URLs, provider secrets, signatures, raw payment payloads, and personally identifiable information out of logs and model prompts.

## Follow the implementation sequence

1. Confirm that `bursar migrate` owns schema installation and that the tenant exists.
2. Validate the candidate `BursarConfig` before publication. Copy `assets/pricing.config.example.yaml` only when the project needs a new complete configuration.
3. Construct one tenant-bound facade and reuse its `credits`, `catalog`, `accounts`, `billing`, and `commerce` capabilities.
4. Connect account creation to the application's durable signup event.
5. Add the selected metering or billing workflow at the application boundary.
6. Map stable `BursarError` codes and categories into the application's existing error surface.
7. Add success, failure, retry, and replay tests before broadening the integration.
8. Update the canonical project documentation if behavior, configuration, or public setup changed.

## Consult canonical sources

Prefer a local checkout because its source matches the application lockfile. Use the live documentation when Bursar is only an installed dependency.

| Task | Repository source | Live documentation |
| --- | --- | --- |
| Install, migrate, create a tenant, and post a first charge | `docs/docs/quickstart.mdx` | `https://zonastery.github.io/bursar/docs/quickstart` |
| Understand accounts, lots, leases, and ledger entries | `docs/docs/concepts/data-model.mdx` | `https://zonastery.github.io/bursar/docs/concepts/data-model` |
| Author configuration and pricing | `docs/docs/concepts/configuration.mdx` and `docs/docs/concepts/pricing.mdx` | `https://zonastery.github.io/bursar/docs/concepts/configuration` |
| Protect idempotency, retries, refunds, and long-running work | `docs/docs/guides/financial-safety.mdx` | `https://zonastery.github.io/bursar/docs/guides/financial-safety` |
| Integrate checkout, subscriptions, webhooks, or auto-recharge | `docs/docs/guides/subscription-integration.mdx` | `https://zonastery.github.io/bursar/docs/guides/subscription-integration` |
| Configure tenant isolation | `docs/docs/guides/multitenancy.mdx` | `https://zonastery.github.io/bursar/docs/guides/multitenancy` |
| Look up exact Python calls | `python/src/bursar/` and generated reference | `https://zonastery.github.io/bursar/docs/python-api` |
| Look up exact TypeScript calls | `javascript/src/` and generated reference | `https://zonastery.github.io/bursar/docs/javascript-api` |
| Inspect the database contract | `python/src/bursar/sql/` | `https://zonastery.github.io/bursar/docs/concepts/database-schema` |

## Verify the result

Run focused tests for the modified application path, then verify these cases:

- Replay the same successful mutation and assert that no second ledger entry exists.
- Reuse the key with a changed payload and assert that the operation fails as a conflict.
- Exercise insufficient credit, quota, entitlement, and concurrency failures without changing the balance.
- Exercise the worker or webhook retry path with the original key.
- For leases, test success, application failure, renewal, expiry, and repeated settlement.
- For provider events, test signature failure, duplicate delivery, out-of-order delivery, and unknown offer mapping.
- Compare the reported balance with the sum of relevant ledger entries in an integration test.
- Test two tenants with reused account and idempotency identifiers and assert isolation.

Report which invariant each test protects. Do not declare the integration complete when only the happy path passes.
