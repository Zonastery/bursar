# Bursar Config Revamp — Implementation Audit

Critical review of the config-schema revamp shipped in commits `bc5f343` (JS) and
`70e569e` (Python) on the `bursar` submodule, measured against
[`docs/revamp/schema-revamp.md`](../schema-revamp.md) and
[`docs/revamp/implementation-plan.md`](../implementation-plan.md).

The review found the **tier→bucket** migration complete and correct, the
**metering** changes (`*` fallback, `calls`, `cache_discount`, `flat_jobs`)
correct in both SDKs, and 11 of 13 Python validation rules enforced. However
the **plans/billing SQL identifiers were not renamed** (boundary-translation
was used instead of the planned full propagation), the **Python billing sync to
SQL is broken**, the **`grant` union is not discriminated**, and **Phase 6
(docs/samples/notebook) is entirely undone**.

Decision locked with maintainer: **finish the full SQL rename** (unreleased
module, fresh-DB, no compat shim). This makes the new→old adaptation layer in
both SDKs unnecessary and it is deleted, yielding a symmetric sync contract.

---

## Findings index

| ID | Severity | Area | Summary |
|----|----------|------|---------|
| C1 | Critical | Python billing | `sync_billing_from_config` passes new-shape `model_dump()` to SQL that reads old keys → subscriptions NOT NULL-violate, topups silently skipped. Uncaught (tests use MemoryBillingStore). |
| C2 | Critical | SQL / setup_pricing | `sync_billing_from_config` reads top-level `subscriptions`/`credit_topups`; new config nests under `billing` → `setup_pricing` billing sync is a silent no-op. |
| C3 | Critical | Models | `SubscriptionGrant` is a flat model, not a discriminated union (plan rule 12 / principle 2.3 violated). |
| H1 | High | Docs | Phase 6 (docs, samples, notebook 15) entirely undone. |
| H2 | High | SQL | Plans/billing SQL columns not renamed (deviation from full propagation); dead columns + fallback code. |
| H3 | High | Config | Plan-reference check skipped when `plans` is None. |
| H4 | High | JS parity | JS write-path still uses `tier`; Python uses `bucket` (parity defect). |
| M1 | Medium | JS config | JS silently accepts old deprecated keys as aliases (plan forbids compat shim). |
| M2 | Medium | JS engine | `calcModel` skips prefix-match; `resolveModel` is dead code (spec gap). |
| M4 | Medium | Python config | `BillingSection.subscriptions/topups` typed `dict[str, Any]` + hand-validated (not the typed `BillingConfig`). |
| M5 | Medium | Python config | `ConfigError(Exception)` not `ValueError` → mixed raw/Pydantic errors. |
| M6 | Medium | Docs/comments | Stale doc comments referencing old names. |
| M7 | Medium | Python config | Redundant validation; `>=0`/`>0` constraints split off-model. |
| L1 | Low | JS naming | `getTierBalance`/`adjustTierBalance`/`tierWalkOrder` private helpers un-renamed. |
| L2 | Low | JS naming | `fixedCredits` field/var still "fixed". |
| L3 | Low | Naming | `signup_bonus` tx-type + `grant_signup_bonus()` trigger retained (consistent; optional rename). |
| L4 | Low | JS types | `PricingConfigData` is a benign `@deprecated` alias (delete optional). |

---

## File index

| File | Covers |
|------|--------|
| [01-critical-fixes.md](01-critical-fixes.md) | C1, C2, C3 + end-state symmetric sync design |
| [02-sql-rename.md](02-sql-rename.md) | Step 1 — full SQL rename (004/013/014) + Step 3 — Python store fallback cleanup [H2, C1, C2] |
| [03-python-config.md](03-python-config.md) | Step 4 — Python config hardening [H3, M4, M5, M7, C3-validation] |
| [04-javascript-parity.md](04-javascript-parity.md) | Step 5 — JS adaptation deletion, tier→bucket write-path, drop aliases, prefix-match [C1, H4, M1, M2] |
| [05-tests-parity.md](05-tests-parity.md) | Step 6 — parity fixture cases + real-DB billing sync test |
| [06-docs-samples.md](06-docs-samples.md) | Step 7 — Phase 6 docs/samples/notebook [H1] |
| [07-cosmetic.md](07-cosmetic.md) | Step 8 — naming + stale-comment sweep [M6, L1–L4] |
| [08-verification.md](08-verification.md) | Acceptance criteria, commands, grep guard |

---

## End-state design (resolves C1, C2, H2)

With the SQL fully renamed, the sync contract becomes **symmetric** and the
new→old adaptation layer is deleted from both SDKs:

- `sync_billing_from_config(p_billing JSONB)` expects a **BillingConfig-shaped**
  JSON: top-level `currency` / `subscriptions` / `topups`, each offer in the new
  nested shape (`plan`, `grant.{mode,credits,bucket,replace_prior}`,
  `providers`), each topup in the new shape (`deposit_to`, `credits_per_unit`,
  `providers`).
- `setup_pricing` calls `PERFORM sync_billing_from_config(p_config->'billing')`
  (extracts the billing section).
- Python billing stores pass `config.model_dump()` **directly** (no adaptation).
- JS billing stores pass `config` **directly** (adaptation layer deleted).

The `grant` discriminated union (C3) is a model-level concern; SQL keeps flat
renamed columns (`grant_mode`, `grant_credits`, `grant_bucket`,
`grant_replace_prior`). `sync_billing_from_config` reads the nested config
`grant.*` keys and writes the flat columns — the same pattern `sync_plans` uses
for `safety.*` → flat `billing_mode` etc.

---

## Sequencing

1. **Commit A (coupled, money-adjacent, riskiest):** Steps 1+2+3 — SQL rename
   (004/013/014) + Python billing models/stores + Python interface-store
   fallback cleanup. Both sides of the sync contract change together, so the
   suite stays green. Guard: invariant/property tests + new real-DB billing test.
2. **Commit B:** Step 4 — Python config hardening.
3. **Commit C:** Step 5 — JS parity (adaptation deletion must land with Commit A
   if JS is exercised against the renamed SQL in the same suite; otherwise
   immediately after).
4. **Commit D:** Step 6 — parity fixture + tests.
5. **Commit E:** Step 7 — docs/samples/notebook.
6. **Commit F:** Step 8 — cosmetic sweep.

> Commit A and the JS adaptation deletion (Step 5) change both sides of the
> sync contract and **must not be split across a green-suite boundary**. Land
> them together or in immediate succession.

---

## Locked decisions

- **SQL strategy:** finish full rename (renamed columns, drop dead `feature_limits`
  and per-topup `currency`). No backward-compat shim. Fresh-DB only.
- **`signup_bonus` tx-type:** leave as-is (consistent across SDK + SQL trigger);
  rename is optional and out of scope.
- **`PricingConfigData` (JS):** delete (deprecated, zero internal usages).
- **`offer_key` / `topup_key` / `plan_key` SQL PKs:** kept — they store the
  config map key; the plan's "no duplicated identifier" rule is about the
  config/model shape, not the SQL PK.
