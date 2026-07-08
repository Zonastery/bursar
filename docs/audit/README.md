# Bursar Billing Module Audit

> **Scope**: Audit of the subscription management and payment provider integration code on the `feat/subscription-management` branch (~9,800 lines across Python + JavaScript SDKs).
>
> **Status**: Internal engineering document. Not part of the public Docusaurus documentation site.
>
> **Methodology**: Findings were gathered via parallel code-audit tasks and verified against the current branch state (July 2026). Every file:line citation and code snippet has been confirmed against the working tree.

---

## Fix Status Summary

**32 of 35 findings have been fixed.** 3 remain as documentation-only items (architectural or SDK-limitation — not fixable in code).

| Status | Count |
|---|---|
| ✅ Fixed — code changed, all tests pass | 32 |
| ❌ Not fixable — architectural/SDK limitation | 3 |
| **Total findings** | **35** |

| Severity | Total | Fixed | Not fixable |
|---|---|---|---|
| CRITICAL | 2 | 2 | 0 |
| HIGH | 5 | 5 | 0 |
| MEDIUM | 10 | 10 | 0 |
| LOW | 18 | 15 | 3 (#22 Dodo SDK limitation, #27 async vs sync, #28 ResolveUser injection) |

---

## Severity Matrix

All findings ranked by severity. The **Fix Status** column shows the current state. Click through to the detailed file for code snippets, impact analysis, and fix details.

### CRITICAL

| # | Finding | Location | Fix Status | File |
|---|---|---|---|---|
| 1 | JS supabase billing store returns snake_case keys from RPCs but manager reads camelCase — offers/topups silently fail, plans never provisioned for Stripe webhooks | `javascript/src/billing/supabase-billing-store.ts:35`, `javascript/src/billing/billing-manager.ts:224` | ✅ Fixed | [02](./02-supabase-store-key-casing-bug.md) |
| 2 | Python supabase store `compute_topup_credits` reads camelCase key `creditsPerMajorUnit` but RPC returns snake_case `credits_per_major_unit` — always falls back to 1000, ignoring custom topup config | `python/src/bursar/billing/supabase.py:217` | ✅ Fixed | [02](./02-supabase-store-key-casing-bug.md) |

### HIGH

| # | Finding | Location | Fix Status | File |
|---|---|---|---|---|
| 3 | `lookupKey` fallback exists in JS manager but NOT Python — Python cannot handle Dodo/Mock webhooks that rely on `plan_slug` metadata | `javascript/src/billing/billing-manager.ts:228-235` vs `python/src/bursar/billing/manager.py:119-132` | ✅ Fixed | [07](./07-parity-violations.md) |
| 4 | Python ships only `PaymentProvider` interface — zero Stripe/Dodo/Mock provider implementations. JS has all three. | `python/src/bursar/providers/types.py` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 5 | Stripe event-mapper fans out `checkout.session.completed` into 3 independent `handleEvent` calls — if 2nd/3rd fails, 1st is committed, no rollback | `javascript/src/providers/stripe/event-mapper.ts:49-139` | ✅ Fixed | [03](./03-error-prone-code.md) |
| 6 | `processing` billing events left by crashes are never retried — no sweep mechanism. `claim_billing_event` returns `retry` but nothing acts on it | `javascript/src/billing/billing-manager.ts:51-70` | ✅ Fixed | [03](./03-error-prone-code.md) |
| 7 | Dodo `listPaymentMethods` returns `[]` — stub that ignores the SDK | `javascript/src/providers/dodo/provider.ts:119-121` | ✅ Fixed | [01](./01-missing-implementations.md) |

### MEDIUM

| # | Finding | Location | Fix Status | File |
|---|---|---|---|---|
| 8 | Mock provider declares `provider = "dodo"` — all mock billing rows indistinguishable from real Dodo data in production tables | `javascript/src/providers/mock/provider.ts:15` | ✅ Fixed | [04](./04-red-flags.md) |
| 9 | `resolve_billing_offer_by_price` returns NULL for both "not found" AND "permission failure" — `lookupKey` fallback could grant wrong plan on RLS misconfiguration | `python/src/bursar/sql/013_billing.sql:581-583` | ✅ Fixed | [04](./04-red-flags.md) |
| 10 | `set_active_pricing_config` swallows billing sync errors with `RAISE WARNING` — pricing succeeds, billing silently fails | `python/src/bursar/sql/013_billing.sql:816-820` | ✅ Fixed | [04](./04-red-flags.md) |
| 11 | `lookupKey` fallback synthesizes fake offer `{planKey: lookupKey, entitlementMode: "allowance"}` — bypasses offer resolution, treats metadata string as plan key | `javascript/src/billing/billing-manager.ts:228-235` | ✅ Fixed (added to Python) | [06](./06-hacks-workarounds.md) |
| 12 | 33 `BillingEventType`s defined; only ~10 emitted by mappers. 23 are aspirational. Handler dispatch silently ignores unknowns. | `javascript/src/billing/billing-types.ts:10-45` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 13 | `datetime.fromisoformat(ps)` in `_provision_subscription` — no try/except, crashes on malformed dates | `python/src/bursar/billing/manager.py:569` | ✅ Fixed | [03](./03-error-prone-code.md) |
| 14 | `_handle_customer_updated` / `_handle_customer_deleted` are no-ops — return `handled: true` without persisting anything | `python/src/bursar/billing/manager.py:238-242` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 15 | Refund clawback uses default 1000 `creditsPerMajorUnit` when payment metadata is missing — silently claws back wrong amount | `python/src/bursar/billing/manager.py:513`, `javascript/src/billing/billing-manager.ts:589-605` | ✅ Fixed | [03](./03-error-prone-code.md) |
| 16 | JS `BillingManager.handleEvent` silently catches all exceptions → `{handled: false}` with stringified error, no retry, no alerting | `javascript/src/billing/billing-manager.ts:62-68` | ✅ Fixed | [03](./03-error-prone-code.md) |

### LOW

| # | Finding | Location | Fix Status | File |
|---|---|---|---|---|
| 17 | `computeTopupCredits` formula duplicated across 7 store impls (4 JS + 3 Python) — should be a shared utility | All billing store files | ✅ Fixed | [05](./05-optimization-refactor.md) |
| 18 | Event handler dispatch dict rebuilt on every `routeEvent` / `_route_event` call — should be class-level constant | `python/src/bursar/billing/manager.py:71-99`, `javascript/src/billing/billing-manager.ts:72-101` | ✅ Fixed | [05](./05-optimization-refactor.md) |
| 19 | Synthetic event-ID suffixing (`${event.id}_checkout`, `${event.id}_sub`, `${event.id}_sub_activated`) — works but fragile | `javascript/src/providers/stripe/event-mapper.ts:52,70,97` | ✅ Fixed | [06](./06-hacks-workarounds.md) |
| 20 | `JSON.parse(JSON.stringify(config))` deep-clone hack in supabase store to strip undefined | `javascript/src/billing/supabase-billing-store.ts:19` | ✅ Fixed | [06](./06-hacks-workarounds.md) |
| 21 | `_coalesce` helper papering over inconsistent data in subscription state merge | `python/src/bursar/billing/manager.py:24-26` | ✅ Fixed | [06](./06-hacks-workarounds.md) |
| 22 | Dodo payment-method-update via checkout session creation — no native Dodo API for this | `javascript/src/providers/dodo/provider.ts:87-101` | ❌ Dodo SDK limitation | [06](./06-hacks-workarounds.md) |
| 23 | `claimBillingEvent` defensive `{status: "retry"}` fallback with no retry consumer | `javascript/src/billing/supabase-billing-store.ts:49` | ✅ Fixed | [06](./06-hacks-workarounds.md) |
| 24 | Stripe `createPaymentMethodSetupSession` identical to `createUpdatePaymentMethodSession` — both use `payment_method_update` flow | `javascript/src/providers/stripe/provider.ts:67-91` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 25 | `resolveOfferAndKeys` calls store on every subscription event — no offer cache | `javascript/src/billing/billing-manager.ts:216-220` | ✅ Fixed | [05](./05-optimization-refactor.md) |
| 26 | Stripe event-mapper retrieves subscription twice in `invoice.paid` flow — webhook payload + SDK retrieve | `javascript/src/providers/stripe/event-mapper.ts:214` | ✅ Fixed | [05](./05-optimization-refactor.md) |
| 27 | JS `handleEvent` is `async`; Python `handle_event` is synchronous — different concurrency surfaces | `javascript/src/billing/billing-manager.ts:51` vs `python/src/bursar/billing/manager.py:51` | ❌ Architectural | [07](./07-parity-violations.md) |
| 28 | JS `PaymentProvider` interface has `ResolveUserCallback`; Python has `ResolveUserFn` on `BillingManager` instead — different injection points | `javascript/src/providers/types.ts:51-54` vs `python/src/bursar/billing/manager.py:21` | ❌ Architectural | [07](./07-parity-violations.md) |
| 29 | Dodo `createCustomerPortalSession` doesn't pass `return_url` to SDK | `javascript/src/providers/dodo/provider.ts:42` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 30 | Dodo `createCustomer` doesn't pass `metadata` to SDK | `javascript/src/providers/dodo/provider.ts:125-128` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 31 | Dodo `getInvoiceUrl` reads `payment.payment_link` but Dodo `Payment` type may not have this field | `javascript/src/providers/dodo/provider.ts:134-135` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 32 | `_handle_trial_will_end` is a no-op notification — no email/webhook is sent to the user | `python/src/bursar/billing/manager.py:395-396` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 33 | `camelToSnake` in postgres store silently lowercases all keys — provider names like "Stripe" become "stripe" | `javascript/src/billing/postgres-billing-store.ts:63-67` | ✅ Fixed | [04](./04-red-flags.md) |
| 34 | `BillingEventType` includes events with no handler: `checkout.expired`, `invoice.created`, `invoice.finalized`, etc. — silently ignored | `javascript/src/billing/billing-types.ts:10-45` | ✅ Fixed | [01](./01-missing-implementations.md) |
| 35 | Python `BillingManager` reads snake_case offer keys (`offer.get("offer_key")`); JS reads camelCase (`offer.offerKey`) — both correct for their ecosystem but the supabase store bug affects them differently | `python/src/bursar/billing/manager.py:132` vs `javascript/src/billing/billing-manager.ts:224` | ✅ Fixed | [07](./07-parity-violations.md) |

---

## File Index

| File | Findings | Category | Fixed |
|---|---|---|---|
| [01-missing-implementations.md](./01-missing-implementations.md) | 4, 7, 12, 14, 24, 29, 30, 31, 32, 34 | Stubs, no-ops, aspirational types | 10/10 fixed ✅ |
| [02-supabase-store-key-casing-bug.md](./02-supabase-store-key-casing-bug.md) | 1, 2 | CRITICAL: snake_case ↔ camelCase mismatch | 2/2 fixed ✅ |
| [03-error-prone-code.md](./03-error-prone-code.md) | 5, 6, 13, 15, 16 | Non-atomic fan-out, silent failures, parsing risks | 5/5 fixed ✅ |
| [04-red-flags.md](./04-red-flags.md) | 8, 9, 10, 33 | Mock identity lie, permission/NULL conflation, swallowed errors | 4/4 fixed ✅ |
| [05-optimization-refactor.md](./05-optimization-refactor.md) | 17, 18, 25, 26 | DRY violations, repeated lookups, dispatch dict rebuild | 4/4 fixed ✅ |
| [06-hacks-workarounds.md](./06-hacks-workarounds.md) | 11, 19, 20, 21, 22, 23 | lookupKey fallback, synthetic event IDs, clone hack | 5/6 fixed (1 SDK limitation) |
| [07-parity-violations.md](./07-parity-violations.md) | 3, 27, 28, 35 | Python ↔ JS divergences | 2/4 fixed (2 architectural) |

---

## Architecture Context

The billing module adds two new layers on top of bursar's existing credit engine:

```
PaymentProvider (Stripe/Dodo/Mock)  — translates provider webhooks → normalized BillingEvents
        ↓
BillingManager                      — event state machine: claim → route → complete/fail
        ↓                              ↓ (side effects)
BillingStore (Memory/PG/Supabase)   CreditManager (existing engine)
```

- **`billing/`** — provider-agnostic subscription + payment lifecycle (models, store, manager)
- **`providers/`** — payment-provider adapters (interface + Stripe/Dodo/Mock impls)
- **`sql/013_billing.sql`** — 9 tables, 12 RPCs, all RLS-locked
- **`sql/014_cycle_grant.sql`** — cycle grant revocation RPC
- **`sql/015_billing_records.sql`** — payments/refunds/invoices/disputes tables

The parity rule from the credit engine applies: `MemoryStore` is the reference; `PostgresStore` and `SupabaseStore` must match. Python and JS must match.
