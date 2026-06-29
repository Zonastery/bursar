# ducto — Codebase Audit Report

**Scope:** Python SDK, TypeScript/JavaScript SDK, SQL migrations, test suites, docs site, CI/CD, and repo hygiene.
**Method:** 7 parallel reviewers, one per coherent slice; the highest-impact findings were independently re-verified against the source. ducto is a usage-based **credit/billing engine**, so *money-safety, atomicity, and sandbox security* are weighted highest.

> **Bottom line:** The library is well-structured and the AST sandbox's *structural* defenses are mostly sound, but the credit-deduction pipeline is **not transactionally safe**, the in-memory store has **no locking**, the two SDKs **diverge in ways that produce different bills**, one team-attribution SQL function is **outright broken**, and there are **no concurrency / parity / sandbox-escape tests** to catch any of it. Several are direct revenue-leak or double-spend paths.

---

## Cross-cutting themes (the root causes behind most findings)

1. **Money is computed in binary floats.** Both engines evaluate pricing in IEEE-754 `float`/`number`, then `round()` and `int()`/`Math.trunc()`. This loses precision, leaks revenue on sub-credit operations, and — because Python uses banker's rounding while JS uses half-up — **bills differently across SDKs**. Money math should be `Decimal`/integer-minor-units with one explicit rounding mode.
2. **The deduct flow is a non-atomic sequence of independent store calls** (calculate → consume allowance → check cap → reserve → deduct) with no spanning transaction and no compensation on partial failure. Idempotency is also checked *last*. This is the core money-safety defect and the source of the worst Criticals.
3. **The three stores are not behaviorally interchangeable.** MemoryStore vs SQL diverge on locking, reserve/min-balance semantics, expiry double-sweep, and plan-key-vs-id resolution — yet they're documented as drop-in replacements, so behavior changes silently by backend.
4. **The two SDKs are not enforced to agree.** No parity harness exists; concrete divergences already ship (rounding, `tier/clamp/if` arity, number parsing, `% 0`, team idempotency, prototype-chain identifiers).
5. **The test suite cannot catch the dangerous bugs.** Zero concurrency tests, thin sandbox-escape coverage, no NaN/Inf guards tested, "integration" tests that are mocks or silently skipped, and many truthiness-only assertions.
6. **Docs / metadata have drifted from the code** (18 methods/10 migrations vs actual 28/13; `__version__` 0.1.2 vs 1.0.3; stray temp file; release pipeline that can't publish npm).

---

## Severity summary

| # | Severity | Finding | Area |
|---|----------|---------|------|
| C1 | Critical | Non-atomic deduct + idempotency checked too late → double-consumed allowance, stranded reservations, cap double-count on retry/failure | PY manager |
| C2 | Critical | MemoryStore has **zero locking** → races/double-spend; inconsistent with SQL stores | PY + JS stores |
| C3 | Critical | `deduct_credits` ignores reservation amount/existence → reservation guard defeated, concurrent over-deduction | SQL + memory |
| C4 | Critical | `get_team_members` joins non-existent `ct.team_id` column → errors on every call | SQL 008 |
| C5 | Critical | Expression DoS via `**` + uncaught `OverflowError` (`9**9**9` hangs/OOMs) | PY expr |
| C6 | Critical | JS sandbox: prototype-chain identifiers (`__proto__`, `constructor`, `hasOwnProperty`) bypass validation → `NaN`/garbage costs | JS expr |
| C7 | Critical | `NaN`/`Infinity` flow into charges unguarded (JS `Infinity` is charged; div/mod-by-zero) | PY + JS engine |
| C8 | Critical | DB credentials passed as CLI arg / on Make command line → leaked via process list, shell history, CI logs | PY CLI |
| H1 | High | Float money math + rounding divergence (banker's vs half-up) + `int()`/`trunc()` revenue leak on sub-credit ops | PY + JS |
| H2 | High | Spend-cap is a racy read-then-act (TOCTOU) and doesn't accumulate prior window spend → bypassable | PY + JS manager |
| H3 | High | Refund orchestration ignores `result.error` and emits success events for failed refunds; no failure events anywhere | PY + JS manager |
| H4 | High | `expire_credits` SQL never marks swept grants → re-expires same credits each run (double-debit); diverges from MemoryStore | SQL 006 |
| H5 | High | Migration runner commits per-file and continues past failures → committed half-built schema; no overall transaction; enum `ADD VALUE` co-located with use | PY stores |
| H6 | High | `tier`/`percentile`/`clamp`/`if` arity & range validation gaps → silent wrong prices or uncaught `IndexError` (both SDKs, divergent) | PY + JS expr |
| H7 | High | `pricing set` retries *any* exception 15× → can create duplicate immutable pricing versions; masks permanent errors | PY CLI |
| H8 | High | Dockerfile: root user, unpinned base, `COPY . .` with no `.dockerignore` → bakes `.env`/publish token into a layer | PY pkg |
| H9 | High | `__version__` (0.1.2) ≠ pyproject (1.0.3) → `make release` tags wrong version, runtime reports wrong version | PY pkg |
| H10 | High | JS `publishPricing` still fire-and-forget `void` (the bug class just fixed for `publishPricingFromDict`) | JS manager |
| H11 | High | JS number tokenizer accepts `1.2.3` (silently → 1.2); Python rejects → divergent parse | JS expr |
| H12 | High | `deductTeam` has no idempotency key (JS lacks it entirely) → duplicate team charges on retry | JS manager |
| H13 | High | CI release publishing broken/unsafe: `NODE_AUTH_TOKEN: ""`, no environment protection on OIDC publish job, cancellable release | CI |
| H14 | High | SECURITY.md tells reporters to open a **public** issue; no private channel | repo |
| H15 | High | CI: single Python/Node version despite advertised 3.11–3.13 / Node 18+; mutable-tag action pinning + version inconsistency across workflows | CI |
| H16 | High | Idempotency check+insert not atomic and not user-scoped (cross-user key collision, uncaught unique-violation) | SQL 002 |
| H17 | High | `PostgresStore.setup()` (JS) is a no-op returning `success:true` → silent missing schema | JS stores |
| H18 | High | `list_user_transactions`/`list_usage_events` (012/013) omit `REVOKE` → callable by `anon`/`authenticated`, leaking arbitrary users' history on Supabase | SQL 012/013 |
| M1 | Medium | `reserve_credits` min-balance/capping semantics differ between SQL and MemoryStore | stores |
| M2 | Medium | Team `total_spent`: lifetime counter (cap check) vs monthly recompute (display) — two sources of truth | SQL 008 |
| M3 | Medium | Engine `total` vs `CostBreakdown` validator recompute = two sources of truth for the key number; `total=` arg is dead | PY engine |
| M4 | Medium | `if(` rewrite regex has no `\b` → mangles identifiers ending in `if` | PY expr |
| M5 | Medium | Config validation doesn't check expression variable names against the real metric set → typos fail at runtime, not load | PY + JS expr |
| M6 | Medium | `check_feature` uses `bool(value)` → numeric `0`/`""`/`false` entitlements read as absent (contradicts documented convention; SDKs diverge) | PY + JS |
| M7 | Medium | `CreditMetadata(extra="allow")` lets caller metadata overwrite system fields (`idempotency_key`, `model`, `breakdown_total`) | PY manager |
| M8 | Medium | `CapCheckResult.action` is untyped `str` → unknown/None action fails **open** (allows spend) | PY manager |
| M9 | Medium | Timezone handling: naive `datetime.now()` for events; MemoryStore string-compares ISO timestamps → wrong windows / `TypeError` on tz-aware input; SQL uses session TZ for day buckets | PY |
| M10 | Medium | Supabase `_rpc` can't distinguish error envelope from data; several callers ignore `error` key; non-HTTP `httpx` errors leak raw | PY stores |
| M11 | Medium | Money stored as `INTEGER` (overflow risk); `credits_add` does no sign/zero validation | SQL |
| M12 | Medium | CLI uses hand-rolled `args[]` slicing (brittle `--label`, `int()` `ValueError` tracebacks, incomplete file-load error handling) instead of `argparse` | PY CLI |
| M13 | Medium | pyproject: `supabase` extra needlessly pulls `psycopg2-binary`; dev deps duplicated with version skew; `requires-python` unbounded | PY pkg |
| M14 | Medium | `activate/set_active_pricing` version assignment is a read-then-write race | SQL 003 |
| M15 | Medium | `lefthook` autofix with `stage_fixed:true` silently stages unreviewed changes; hooks bypassable, CI is the only real gate | repo |
| M16 | Medium | Stale docs: "18 abstract methods / 10 migrations" (actual 28 / 13); `loadPricingFile` doc import from wrong entry point; `gen-api-docs.sh` malformed `printf` → broken Sphinx | docs |
| M17 | Medium | Committed stray `01_pricing_basics.mdx.tmp`; generated docs checked into source | docs |
| M18 | Medium | `low_balance` threshold `min_balance * 2` is an undocumented magic number, level-triggered → event spam every call near threshold | PY manager |
| L1–L13 | Low | See "Low" section (dead exports, negative `addCredits`, `deduct_fixed` unknown-job free, Make portability, `.gitignore` gaps, CoC/issue-template placeholders, weak test assertions, etc.) | all |

---

## CRITICAL

### C1 — Non-atomic deduct pipeline; idempotency checked too late
**`python/src/ducto/manager.py:240–343`** (JS mirrors at `javascript/src/manager.ts:174–274`)
`deduct()` is four independent store round-trips with no spanning transaction:
`check_allowance`/`increment_usage_window` (244) → `check_spend_cap` (269) → `reserve_credits` (318) → `deduct_credits` (332). Idempotency is only honored *inside* `deduct_credits` (key passed at 336). Consequences:
- **Allowance double-spend on retry:** a retried request with the same `idempotency_key` re-runs `increment_usage_window` (244) and burns free allowance again before the replay is recognized at 332.
- **Allowance leak on failure:** if `reserve_credits`/`deduct_credits` fails after allowance was consumed (`cost -= consume`), the window is never rolled back — the user loses free allowance for an operation that was never charged.
- **Stranded reservation:** on `deduction.error` the code raises (341) without releasing the reservation; on retry, `reserve_credits` (not idempotency-keyed) reserves *again*, compounding locked credits until the 10-min expiry.

**Fix:** Push calculate→allowance→cap→reserve→deduct into a single server-side transactional RPC, idempotency-keyed end-to-end; or add compensation (restore the usage window, release the reservation) on every failure path. Check idempotency *first*.

### C2 — MemoryStore has no locking → races / double-spend
**`python/src/ducto/interface/memory.py`** (e.g. `reserve_credits:187`, `deduct_credits:236`, `deduct_team:790`); **`javascript/src/stores/memory-store.ts:178`**
Every mutation is a plain read-modify-write on a dict (`self._balances[user_id] = current - amount`) with **no lock of any kind**. The SQL stores rely on `SELECT … FOR UPDATE`; MemoryStore has no equivalent, so under concurrency it can go negative / double-spend, and it does **not** match the SQL stores' guarantees. There are also **no concurrency tests** to catch this (the "concurrent" tests are sequential `await`s).

**Fix:** Guard all mutating/reading methods with a `threading.RLock` (Python) / serialize (JS), or loudly document single-threaded-only. Add concurrent-deduct tests asserting total debited never exceeds starting balance.

### C3 — `deduct_credits` ignores the reservation amount → guard defeated
**`python/src/ducto/sql/002_credit_rpcs.sql:128–223`**, mirrored in `memory.py:236–271`
`reserve_credits` locks the row and caps the reservation to available balance, but `deduct_credits` never validates `p_amount` against the reserved amount, nor that the reservation exists/is unexpired — it only checks raw balance and deletes the reservation row by id. So `reserve_credits(10)` then `deduct_credits(1000)` succeeds; and two concurrent reserve/deduct flows each pass their independent balance check and **over-deduct** the same credits. The reservation provides no real spend ceiling.

**Fix:** In `deduct_credits`, lock and validate the reservation (`p_amount <= reservation.amount`, unexpired), or deduct against `balance − active_reservations`. The reservation row must be the authority on the maximum deductible amount.

### C4 — `get_team_members` references a non-existent column
**`python/src/ducto/sql/008_team_balances.sql:153`**
`LEFT JOIN public.credit_transactions ct ON ct.user_id = tm.user_id AND ct.team_id = p_team_id` — but `credit_transactions` (`001_credit_tables.sql`) has **no `team_id` column** (the team id lives in `metadata`). *Verified:* `grep team_id 001_credit_tables.sql` → no match. The function raises `column ct.team_id does not exist` on every call, breaking team spend attribution.

**Fix:** Either add a real `team_id UUID` column (and index) to `credit_transactions`, or change the predicate to `ct.metadata->>'team_id' = p_team_id::text`. Reconcile with the `credit_team_members.total_spent` counter (see M2).

### C5 — Expression DoS via `**` and uncaught `OverflowError`
**`python/src/ducto/expr.py:27` (`ast.Pow` allowlisted), `319–324`**
Only `ZeroDivisionError` is caught around `eval`; `ast.Pow` is unbounded. *Verified:* `9 ** 9 ** 9` allocates a multi-GB integer and hangs/OOMs; `input_tokens ** 400.0` raises an **uncaught `OverflowError`** that escapes the engine. Pricing expressions come from the DB `credit_pricing_config` table — a real trust boundary.

**Fix:** Remove `**` or require a small constant exponent; catch `OverflowError`/`ValueError` and convert to `ExpressionError`; reject non-finite results.

### C6 — JS sandbox: prototype-chain identifiers bypass validation
**`javascript/src/expr.ts:487` (`!(n.name in variables)`), `503` (`vars[node.name] ?? 0`)**
The `in` operator walks the prototype chain, so `__proto__`, `constructor`, `toString`, `hasOwnProperty` are **not** rejected as undefined variables, and evaluation returns the inherited member. *Verified:* `__proto__ * 1` → `NaN`; `hasOwnProperty + 1` → `"function hasOwnProperty() { [native code] }1"` (a **string** cost). Python rejects all of these. This is both a robustness/security hole and a hard parity break that corrupts billing.

**Fix:** Use `Object.prototype.hasOwnProperty.call(variables, n.name)` (or a null-prototype object / `Map`); reject any non-own key. Apply the same in `validateVariables` (414).

### C7 — `NaN`/`Infinity` flow into charges unguarded
**`javascript/src/expr.ts:522–528`, `engine.ts:10–12`, `manager.ts`; `python/src/ducto/expr.py:322–323`**
JS division-by-zero → `Infinity`, modulo-by-zero → `NaN`; `safeTotal(NaN)` stays `NaN`, `safeTotal(Infinity)` stays `Infinity`. In the manager, `NaN > 0` is false (silent free usage) but **`Infinity > 0` is true → `Math.trunc(Infinity)` = `Infinity` is charged**. No `isFinite` guard anywhere. Python maps div-by-zero to `inf` (`expr.py:323`), which flows through `_safe_total` as an infinite cost. The two SDKs also disagree on `% 0` (Python `inf` vs JS `NaN`).

**Fix:** Assert `Number.isFinite`/`math.isfinite` on each evaluated expression and on `total`; raise `ExpressionError` otherwise. Don't map div-by-zero to `inf`.

### C8 — DB credentials on the command line
**`python/README.md:89`, `python/src/ducto/__main__.py:72–91`, root `Makefile:9,32`**
`ducto migrate "postgresql://user:pass@host/db"` passes the password as a positional CLI arg → visible in `ps`/`/proc/<pid>/cmdline`, recorded in shell history, and leaked into CI logs. The root Makefile likewise interpolates `PG_PASS` onto the command line.

**Fix:** Read the connection string from an env var (like the `SUPABASE_*` path already does) or stdin/file; document env-var as primary; pass URLs via environment in Make, not on the command line.

---

## HIGH

### H1 — Float money math, rounding divergence, and truncation revenue leak
**`engine.py:17–19,78–83`, `manager.py:235–236`; `expr.ts:546`, `engine.ts:11,44`, `manager.ts:170`**
All cost math is float. *Verified:* `input_tokens*0.1 + output_tokens*0.2` (both 1) → `0.30000000000000004`. Python `round()` is banker's rounding; JS `Math.round` is half-up — `round(2.5)` is `2` (Py) vs `3` (JS): **same config, different bill**. Worse, `cost = int(breakdown.total)` / `Math.trunc(...)` truncates toward zero, so any operation costing `< 1` credit is charged **0** with no carryover — unbounded revenue leakage at scale (the "consumer-friendly" comment masks this).
**Fix:** Use `Decimal`/integer-minor-units with one documented rounding mode (e.g. `ROUND_HALF_UP` or per-event `ceil`) shared by both SDKs; or accumulate fractional remainder per user. Add a shared rounding/truncation test table.

### H2 — Spend cap is a racy read-then-act and ignores prior spend
**`manager.py:268–300`; `manager.ts:200–230`**
`check_spend_cap` reads, decides, and the manager raises — but the spend counter is only updated later by the deduction. N concurrent `deduct()` calls each see `current_spend` below the limit and all proceed (classic TOCTOU), collectively blowing past `cap_limit`. Tests never seed prior window spend (`memory-store.test.ts:629` sets `currentSpend: 0`), so the accumulation path is untested.
**Fix:** Enforce the cap atomically inside the same locked/transactional deduct operation; treat standalone `check_spend_cap` as a non-authoritative pre-check.

### H3 — Refund/error paths emit false success events; no failure events
**`manager.py:378–408,340–376`; `manager.ts:286–294`**
`refund_credits` never checks `result.error` before emitting `credits.refunded` (with default `amount=0`), so a failed/duplicate/over-refund fires a "success" event and returns no exception. No `credits.deduct_failed`/cap-deny failure event exists anywhere — exactly the events a billing system needs for observability/fraud. `deduct_team` returns errors silently instead of raising (inconsistent with `deduct`).
**Fix:** Guard all success emits on `!result.error`; add failure events; make `deduct_team` consistent with `deduct`.

### H4 — `expire_credits` re-sweeps already-expired grants
**`python/src/ducto/sql/006_credit_expiry.sql:25–66`**
The sweep sums all transactions with `expires_at <= now()` and debits, but **never marks grants as swept**. Next run re-sums them; if fresh credits were added meanwhile, they're clawed back again. MemoryStore *does* null `expires_at` on sweep (`memory.py:512`), so the backends diverge and the SQL one is wrong (financial over-charge).
**Fix:** Mark swept grants (drop `expires_at` / add `swept_at`) so they're excluded next run, matching MemoryStore.

### H5 — Migration runner commits per-file and continues past failures
**`python/src/ducto/interface/postgres.py:65–105`; `supabase.py:46–102`; `sql/008:4`**
`PostgresStore.setup` does `execute; commit` per file and on error appends to a list and **continues** — later files commit onto a half-built schema, with no overall transaction and an easily-ignored error list. `supabase.run_migrations` has no per-file handling but also no spanning transaction. Separately, `008` runs `ALTER TYPE … ADD VALUE 'team_usage'` in the same file that references it (Postgres forbids using a new enum value in the same transaction).
**Fix:** Run all migrations in one transaction (or stop on first error and roll back). Move enum `ADD VALUE IF NOT EXISTS` to its own migration committed before any use.

### H6 — `tier`/`percentile`/`clamp`/`if` validation gaps (both SDKs, divergent)
**`expr.py:83–119`; `expr.ts:547–557`**
- Python `tier()` with an even arg count silently returns a threshold as a default (*verified:* `tier(500,100,1,200)` → `200`); `percentile(p>100)` raises uncaught `IndexError`, `p<0` indexes from the end silently.
- JS does **no** arity checks: `tier(x,5)`→`5`, `if(c,5)`→`5`, `clamp(x)`→`NaN`, `min()`→`Infinity` — all of which Python rejects. A typo'd config validates clean in JS and ships wrong/`NaN` prices.
**Fix:** Validate arities (`if`/`clamp`==3, `tier`>=3 odd, `percentile` range 0–100, `min`/`max`>=1) and raise `ExpressionError` in both SDKs so config-load catches it.

### H7 — `pricing set` retries any exception 15× → duplicate immutable versions
**`python/src/ducto/__main__.py:156–167`**
The retry loop catches bare `Exception` and retries 15× with 2 s sleeps "for PostgREST cache" — but also retries auth/validation/network errors (30 s waits), and because `pricing set` always creates a new immutable version, a write that committed server-side but timed out on the client gets retried and **creates a duplicate version**. Same pattern duplicated across all `pricing` subcommands.
**Fix:** Retry only the specific transient PostgREST/connection error; never blind-retry a non-idempotent write; use bounded backoff.

### H8 — Dockerfile bakes secrets, runs as root, unpinned
**`python/Dockerfile:1–4`** — `FROM python:3.12-slim` (unpinned), `COPY . .` with **no `.dockerignore`** → any local `.env` (Supabase service-role key, publish token) is copied into a layer permanently; runs as **root**; no `USER`/`ENTRYPOINT`; single stage.
**Fix:** Pin base by digest, add `.dockerignore` (`.env`, `.venv`, `dist`, `tests`, `.git`), add non-root `USER` and `ENTRYPOINT ["ducto"]`, consider multi-stage.

### H9 — Version mismatch breaks release tagging
**`python/src/ducto/__init__.py:3` (`0.1.2`) vs `python/pyproject.toml:3` (`1.0.3`)** — *verified.* `python/Makefile:3` derives the release tag from `ducto.__version__`, so `make release` tags `v0.1.2` while PyPI publishes `1.0.3`; `ducto.__version__` reports the wrong number at runtime.
**Fix:** Single-source the version (`importlib.metadata.version` or `[tool.setuptools.dynamic]`).

### H10 — JS `publishPricing` still fire-and-forget
**`javascript/src/manager.ts:102`** — `void this.store.setActivePricing(...)` (not awaited) — the exact race fixed for `publishPricingFromDict` (commit 6388815). The engine updates in memory immediately but the store write/plan-sync isn't awaited, and a rejection becomes an unhandled promise rejection.
**Fix:** Make it `async` and `await` the store call.

### H11 — JS number tokenizer accepts malformed literals
**`javascript/src/expr.ts:146–154`** — greedily consumes `[0-9.]+`, so `1.2.3` → `parseFloat("1.2.3")` = `1.2` silently; Python's `ast.parse` rejects it → divergent parse, silently-wrong price in JS.
**Fix:** Strict numeric regex (`[0-9]+(\.[0-9]+)?`), reject multi-dot / `NaN`.

### H12 — `deductTeam` has no idempotency (JS)
**`javascript/src/manager.ts:301–337`** — no `idempotencyKey` param; Python `deduct_team` has one (`manager.py:415`). Retried team deductions double-charge the shared pool. The store interface also lacks the param (`credit-store.ts:107`).
**Fix:** Thread `idempotencyKey` through `deductTeam` and the store interface to match Python.

### H13 — CI release publishing is broken/unsafe
**`.github/workflows/ci.yml:122–124,95–124,10–12`** — *verified* `NODE_AUTH_TOKEN: ""` (npm publish fails every release while `uv publish` ships → **half-published releases**). The release job has `id-token: write` but no protected `environment:` (any contributor who can push a `v*` tag triggers a publish). `cancel-in-progress: true` applies to tag refs too, so a release can be cancelled mid-publish.
**Fix:** Use npm OIDC trusted publishing or a real `NPM_TOKEN` secret; add a protected `environment`; disable cancellation for tag refs; split Python/npm publish.

### H14 — SECURITY.md directs vuln reports to public issues
**`SECURITY.md:5`** — instructs opening a public GitHub issue (then self-contradictorily says don't, for RCE) and provides **no private channel** — wrong for a sandbox-security-critical billing library.
**Fix:** Enable GitHub Private Vulnerability Reporting and/or a monitored security email; remove the public-issue instruction.

### H15 — CI matrix & action pinning
**`.github/workflows/ci.yml`, `docs.yml`** — every job hardcodes Python `3.11` and Node `22` despite advertised 3.11–3.13 / Node 18+ (no matrix). Actions are pinned to **mutable major tags** (supply-chain risk for an OIDC-publishing repo), and pinning is **inconsistent**: `actions/setup-node@v4` in ci.yml vs `@v6` in docs.yml — at least one is wrong. *(I could not verify whether `checkout@v7`/`setup-python@v6` resolve; confirm against currently-published majors and pin to SHAs.)*
**Fix:** Add Python/Node matrices matching the support claim; pin actions to commit SHAs; make versions consistent across workflows.

### H16 — Idempotency check+insert not atomic / not user-scoped
**`python/src/ducto/sql/002_credit_rpcs.sql:154–210`** — `SELECT … WHERE metadata->>'idempotency_key' = …` then update/insert is read-then-write; two concurrent same-key calls both pass the SELECT, both deduct, and the second hits the (uncaught) unique-violation. The lookup has **no `user_id` predicate**, so a key collision across users returns another user's transaction.
**Fix:** Wrap insert in `EXCEPTION WHEN unique_violation THEN <return original>`; scope the key/index by `user_id`; take the row lock before the idempotency check.

### H17 — JS `PostgresStore.setup()` is a no-op reporting success
**`javascript/src/stores/postgres-store.ts:111–116`** — returns `{success:true, errors:[]}` without running any migrations, so callers get a green setup and missing-RPC errors later.
**Fix:** Implement migration execution or return `success:false`/throw.

### H18 — `list_*` RPCs lack `REVOKE` → data exposure on Supabase
**`python/src/ducto/sql/012_list_transactions.sql`, `013_list_usage_events.sql`** — unlike 001–011, these `SECURITY DEFINER` functions have no `REVOKE EXECUTE … FROM anon, authenticated` and no `auth.uid()` guard, so on a Supabase deployment any authenticated client can read **arbitrary users'** transaction/usage history by passing a `p_user_id`.
**Fix:** Add `REVOKE EXECUTE … FROM anon, authenticated;` and an `auth.uid()`/role guard consistent with the other RPCs.

---

## MEDIUM

- **M1 — Reserve/min-balance semantics differ across stores.** SQL allows reserving down to `min_balance` and silently caps the amount; MemoryStore rejects unless the full amount leaves `min_balance` and never caps. `sql/002:96–109` vs `memory.py:189–197`. Define one semantics + a cross-store conformance test.
- **M2 — Team `total_spent` has two sources of truth.** Cap check uses the lifetime `credit_team_members.total_spent` counter; `get_team_members` reports a month-windowed recompute. `sql/008:187,222` vs `:149`. Pick lifetime or per-period and make both agree.
- **M3 — Engine `total` vs `CostBreakdown` recompute.** `engine.calculate` computes `total` (`engine.py:78`) but `CostBreakdown`'s `model_validator` overwrites it from components (`breakdown.py:31`), and the two clamp differently — two sources of truth for the most important number; the `total=` arg is dead.
- **M4 — `if(` rewrite regex lacks `\b`.** `expr.py:266` `re.sub(r"if\s*\(", …)` mangles any identifier ending in `if` (e.g. `qualif(x)`). Anchor with `\b` or handle in the AST.
- **M5 — No variable-name validation at config load.** `validate_expression` accepts any unknown name as a "variable" (`expr.py:273`; `expr.ts:414`), so `inputtokens * 0.001` validates and only fails at first runtime usage. Pass the known metric set in.
- **M6 — `check_feature` truthiness.** `bool(value)` / `Boolean(value)` makes numeric `0`/`""`/`false` features read as absent, contradicting the documented "numeric ⇒ true" convention; SDKs diverge. `base.py:185`, `manager.py:183`, store JS files. Distinguish presence from truthiness.
- **M7 — `CreditMetadata(extra="allow")` overwrites system fields.** `manager.py:314` merges caller metadata over system-seeded `idempotency_key`/`model`/`breakdown_total`. Merge caller first, system last (or reject reserved keys).
- **M8 — `CapCheckResult.action` untyped, fails open.** `models.py:275` is `str|None`; the manager only branches on `"deny"`/`"warn"`/`"notify"` with no default, so an unexpected value silently allows the spend. Type as `Literal[...]` and default-deny.
- **M9 — Timezone handling.** Events use naive `datetime.now()` (`manager.py:95`); MemoryStore string-compares ISO timestamps (`memory.py:545,649`) → wrong windows / `TypeError` on tz-aware input; SQL `created_at::DATE` uses session TZ → non-deterministic daily buckets. Standardize on tz-aware UTC; compare datetimes, not strings; pin `AT TIME ZONE 'UTC'`.
- **M10 — Supabase `_rpc` error handling.** Can't distinguish an `{"error":…}` envelope from data; `get_active_pricing`/`add_credits`/etc. don't check the `error` key; non-`HTTPStatusError` exceptions leak raw. `supabase.py:135–176`. Centralize: raise `StoreError` on error envelopes; catch `httpx.RequestError`/JSON errors.
- **M11 — Money as `INTEGER`; `credits_add` unvalidated.** Ledger columns are `INTEGER` (overflow ~2.1 B) while analytics use `BIGINT`; `credits_add` accepts negative/zero amounts. `sql/001:27,52`, `sql/002:6–49`. Use `BIGINT`; validate amount sign.
- **M12 — CLI is hand-rolled, not `argparse`.** Brittle `--label` parsing (`__main__.py:148`), `int(args[0])` `ValueError` tracebacks (`:217,237,279`), only `FileNotFoundError` handled on load (`:94–117`). Adopt `argparse` subparsers.
- **M13 — pyproject hygiene.** `supabase` extra pulls `psycopg2-binary` (only `httpx` is needed); dev deps duplicated across `test` extra and `[dependency-groups].dev` with version skew (`pytest-testmon` 2.0 vs 2.2, `httpx` 0.27 vs 0.28.1); `requires-python` unbounded vs the 3.11–3.13 claim. `pyproject.toml:40–104`.
- **M14 — Pricing version assignment race.** `SELECT MAX(version)+1` then `INSERT` without locking (`sql/003:88–97`). Add a unique constraint on `version` and/or advisory-lock during publish.
- **M15 — lefthook autofix.** `stage_fixed:true` silently stages tool-modified files into the commit; real checks only run pre-push and both are bypassable — CI must be the source of truth. `lefthook.yml:5–13`.
- **M16 — Stale/broken docs.** "18 abstract methods / 10 migrations" everywhere (actual **28 / 13** — verified); `configuration.mdx:123` imports `loadPricingFile` from `@apoorwv/ducto` but it's only exported from `/node`; `docs/scripts/gen-api-docs.sh:18` has a malformed `printf` that produces an invalid Sphinx `index.rst`. CONTRIBUTING says "8 abstract methods".
- **M17 — Committed generated/temp files.** `docs/docs/notebooks/01_pricing_basics.mdx.tmp` is tracked; all generated notebook `.mdx` are committed despite being regenerated by `prebuild`. `git rm` the temp file; gitignore generated output.
- **M18 — `low_balance` magic threshold + spam.** `manager.py:364` emits on every deduct at/below `min_balance * 2` (undocumented constant, level-triggered → repeated alerts). Make configurable and edge-triggered.

---

## LOW

- **L1 — `deduct_fixed` unknown job is free.** An unconfigured/typo'd `job_name` returns cost `0` and a "successful" zero-credit deduction (`engine.py:232` → `manager.py:247`). Verify the job exists and raise on unknown.
- **L2 — `addCredits` accepts negative/non-finite amounts** (JS `memory-store.ts:112`, also Python) → can drive balance below `min_balance`, bypassing the floor. Validate finite/positive for `purchase`.
- **L3 — JS `getFixedCost` returns float vs Python `int`** [PARITY]; coerce to int. `engine.ts:94`.
- **L4 — `ImportError` exported but never thrown; `callOrPrimary` is dead indirection.** `errors.ts:17`, `expr.ts:363`. Use or remove.
- **L5 — MemoryStore `setup()` migration list is hardcoded** (Python lists 10, JS lists 9; both omit 011–013) and drifts from `_get_sql_files()`. Derive it.
- **L6 — MemoryStore plan resolution keys on `plan.id` while SQL resolves `plan_key`** (`memory.py:312` vs `sql/004`); `set_user_plan` arg is named `plan_id` but must be a `plan_key` across stores. Align naming/semantics.
- **L7 — Supabase `httpx.Client` never closed; no retry.** `supabase.py:116`. Add `close()`/context-manager + bounded retry for idempotent RPCs. Postgres opens a connection per call (no pooling).
- **L8 — Makefile portability.** Root Makefile relies on `.ONESHELL:` (silently ignored by macOS's GNU make 3.81) so the multi-line `test-js-integration` recipe breaks there; no `help`/`.DEFAULT_GOAL`. Document/require modern `gmake`.
- **L9 — `.gitignore` gaps.** Missing `.venv/`, `.pytest_cache/`, `.coverage`/`htmlcov/`, `.testmondata`, `.env.*` — easy to commit a venv/coverage/secret file.
- **L10 — Repo placeholders/typos.** `CODE_OF_CONDUCT.md:49` `[INSERT CONTACT METHOD]`; bug-report template `about: … ductor's …` (should be "ducto"); FUNDING placeholder lines; default Docusaurus social card. Confirm `apoorwv` is the intended canonical handle across LICENSE/URLs/package scope.
- **L11 — `package.json` (JS) hygiene.** `exports` lists `import` before `types`; no `require`/CJS condition; `pg`/`js-yaml` are dynamic-imported but not declared as optional `peerDependencies`; no `engines.node`.
- **L12 — Pervasive `as`/`any` casts at the JS DB/config boundary** defeat type safety (`config.ts:55`, stores' `row as Record<string, unknown>`, `Number(undefined)`→`NaN`). Validate config value types (zod-style) to match Pydantic; treat DB rows as `unknown` and validate.
- **L13 — Test-suite weaknesses (quality, not gaps already listed):** truthiness-only assertions (`toBeTruthy`, `> 0`, `is not None`) where exact values are deterministic; CLI tests assert substrings, not exit codes/parsed JSON; expiry tests are clock-fragile (`now().replace(second=0)`, `Date.now()+1` + `setTimeout(10)`); shared `user_1` fixture + lower-bound asserts hide state bleed; `supabase-store.test.ts` only asserts network rejects (no URL/header/payload checks); `test_set_active_pricing` is an empty `pass`; no coverage gate or `integration` marker in either suite. **Most dangerous missing tests:** concurrency/double-spend, sandbox-escape table (incl. `**` DoS and JS `__proto__`), NaN/Inf/`%0`, over-refund & cumulative partial refund, expiry double-sweep, cap accumulation, idempotency cross-user/different-amount, and a **cross-SDK parity harness**.

---

## Recommended order of attack

1. **Make the ledger atomic & idempotent first** (C1, C3, H16) — one transactional, idempotency-keyed deduct RPC that also enforces caps (H2) and consumes allowance in the same transaction. This collapses the worst Criticals into one design fix.
2. **Lock MemoryStore (C2)** and **add concurrency + parity + sandbox-escape tests** (test L13) so regressions are caught.
3. **Fix the broken/divergent money math** (H1): `Decimal`/minor-units, one rounding mode, shared across SDKs.
4. **Patch the sandboxes** (C5 `**`, C6 `__proto__`, C7 NaN/Inf, H6 arity, H11 number parsing).
5. **Fix `get_team_members` (C4), `expire_credits` double-sweep (H4), and migration atomicity (H5).**
6. **Lock down release & disclosure** (C8, H7, H8, H9, H13, H14, H18).
7. **Refresh docs/metadata** (H9, M16, M17).

*All Critical and most High findings were re-verified against the source. Parity-divergence items (rounding, arity, parsing, idempotency, prototype-chain identifiers, `%0`) matter specifically because customers can be billed differently depending on which SDK their backend uses.*
