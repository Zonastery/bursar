# 08 — Verification & Acceptance

Run after each commit cluster, and in full before merge. All checks must be
green on a **fresh** Postgres (recreate the DB — there is no data migration).

---

## 8.1 Commands

### Python
```sh
ruff check python/src python/tests
pyright python/src
pytest python/tests            # fresh Postgres: recreate DB first
```
Real-Postgres tests resolve a DSN from `DATABASE_URL` → `BURSAR_TEST_PG_URL` →
a testcontainers `postgres:16` → skip (see `python/tests/conftest.py`). Ensure
Docker is available so the container path runs.

### JavaScript
```sh
npm run typecheck
npm run lint
npm test                       # testcontainers Postgres
```

### Cross-SDK parity
```sh
pytest python/tests/test_config_parity.py
npx vitest run javascript/tests/config-parity.test.ts
```
Both load `tests/parity/config_validation_cases.json` and must agree on every
accept/reject case.

### Notebook (acceptance artifact)
```sh
jupyter nbconvert --to notebook --execute \
  samples/python/notebooks/15_bursar_config_schema.ipynb \
  --output 15_bursar_config_schema.executed.ipynb
```
Must complete top-to-bottom with no errors.

---

## 8.2 Targeted regression checks

- **C1 regression guard:** the new real-DB `test_sync_billing_from_config_postgres`
  case (`05-tests-parity.md` §5.2) passes — offers/topups land in renamed
  columns; no NOT NULL violation; topups are not silently skipped.
- **C2 regression guard:** `setup_pricing` round-trip syncs billing via
  `p_config->'billing'` (assert offers resolvable after `publish_pricing_from_dict`).
- **C3 regression guard:** parity cases `grant_cycle_missing_credits_rejected`
  and `grant_allowance_extra_fields_rejected` are **reject** in both SDKs.
- **H3 regression guard:** `subscription_plan_dangling_no_plans_rejected` is
  **reject** in both SDKs.
- **H4 regression guard:** JS `addCredits`/`deductCredits`/`grantSubscriptionCycle`
  accept `bucket` and reject/ignore `tier`; Python and JS produce identical
  results for a cycle-grant scenario.
- **M2 regression guard:** a model id like `claude-sonnet-4-20250514` resolves
  to the `claude-sonnet-4` prefix key when present (engine parity test in both
  SDKs).

---

## 8.3 Grep guard (no old identifiers remain)

After all commits, these patterns must return **zero** matches in
`python/src` and `javascript/src` (excluding shipped migration filenames and
the `signup_bonus` tx-type):

```sh
rg -n 'plan_key|entitlement_mode|cycle_grant_credits|cycle_grant_tier|cycle_grant_replace_prior|credits_per_major_unit|free_allowance|default_billing_mode|feature_limits|expired_by_tier|tier_breakdown|get_tier_balances|getTierBalances|credit_tiers|sync_tiers_from_config|get_user_credit_tiers|default_ttl_days|this_tool_calls|_default' \
  python/src javascript/src
```

Allowed residuals (verify each):
- `012_feature_limits.sql` — shipped migration filename (cannot rename).
- `signup_bonus` — transaction-type string + `grant_signup_bonus()` SQL trigger
  (intentional, L3).
- `offer_key` / `topup_key` / `plan_key` (on `credit_plans` only) — SQL PKs
  storing the config map key (kept by decision).
- `plan_key` as a Python/JS **iteration variable** over `.items()` (e.g.
  `for plan_key, p in plans.items()`) — benign; consider renaming to `plan_id`
  for clarity but not required.

Any other hit is an incomplete migration and must be fixed.

---

## 8.4 Acceptance criteria (from `implementation-plan.md`)

1. ✅ (after Step 4) Single `BursarConfig` model in Python; no
   `BursarConfigData`.
2. ✅ (after Steps 2.2/3.2/3.3) `subscriptions`/`topups` fully typed; no
   `dict[str, dict]`; no `BillingOffer(offer_key=…)` hand-wrapping; `grant` is
   a discriminated union.
3. ✅ (after Step 1) No identifier duplicated as both dict key and inner field
   (config/model level).
4. ✅ `cache_discount` positive; engine subtracts; total clamped `>= 0`
   (already correct).
5. ✅ `*` fallback and `calls` work in both SDKs; `_default`/`this_tool_calls`
   gone (already correct; M1 removes residual aliases).
6. ✅ (after Step 6) Shared parity fixture green in Python and JS.
7. ✅ (after Steps 1-7) Full test suites + notebook 15 green on a fresh
   database.

---

## 8.5 Commit plan (recap)

| Commit | Steps | Guard |
|--------|-------|-------|
| A | 1 + 2 + 3 (SQL rename + Python billing models/stores + Python interface-store fallback cleanup) + C1/C2/C3 model side | invariant/property tests + new real-DB billing test |
| C | 5 (JS adaptation deletion + tier→bucket + aliases + prefix-match) — same green-suite window as A | JS suite + parity |
| B | 4 (Python config hardening) | Python config tests |
| D | 6 (parity fixture + tests) | both parity runners |
| E | 7 (docs/samples/notebook) | notebook executes clean |
| F | 8 (cosmetic sweep) | full suite |

> Commit A and Commit C change both sides of the `sync_billing_from_config`
> contract and **must not be split across a green-suite boundary**. If the JS
> suite is run against the renamed SQL independently, land A and C together.
