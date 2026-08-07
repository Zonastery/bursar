# Bursar

[![CI](https://github.com/Zonastery/bursar/actions/workflows/ci.yml/badge.svg)](https://github.com/Zonastery/bursar/actions/workflows/ci.yml)
[![Docs](https://github.com/Zonastery/bursar/actions/workflows/docs.yml/badge.svg)](https://github.com/Zonastery/bursar/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/bursar.svg)](https://pypi.org/project/bursar/)
[![PyPI downloads](https://img.shields.io/pypi/dm/bursar.svg)](https://pypi.org/project/bursar/)
[![npm](https://img.shields.io/npm/v/@zonastery/bursar.svg)](https://www.npmjs.com/package/@zonastery/bursar)
[![npm downloads](https://img.shields.io/npm/dm/@zonastery/bursar.svg)](https://www.npmjs.com/package/@zonastery/bursar)
[![License](https://img.shields.io/github/license/Zonastery/bursar.svg)](https://github.com/Zonastery/bursar/blob/main/LICENSE)

<p align="center">
  <img
    src="https://raw.githubusercontent.com/zonastery/bursar/main/docs/static/img/logo.png"
    alt="Bursar logo"
    width="192"
    height="192"
  />
</p>

Bursar is Zonastery's open-source credit-ledger and billing SDK for AI SaaS
platforms. It meters usage, prices operations, manages balances, and bills
customers from one canonical PostgreSQL schema and one versioned configuration
document. The Python and TypeScript SDKs share both, so they produce identical
accounting and identical bills.

## Highlights

- **Canonical accounting** — `credit_accounts.balance` is the only stored
  balance and `credit_ledger_entries` the only monetary history; there are no
  projected transaction or bucket-balance tables.
- **Declarative configuration** — operations, rate cards, plans, allowances,
  and billing live in one strict, versioned document, published through the
  SDK and readable by billing and auto-recharge.
- **Financial safety by default** — reserve-then-settle leases with
  idempotency keys, expiry, and strict-prepaid or overdraft policies.
- **Safe expressions** — an AST-based evaluator with a strict allowlist: no
  `eval`, no arbitrary code execution.
- **Identical behavior in Python and JavaScript** — same config, same
  rounding, same results.

## Quick start

Requirements: Python 3.12+ or Node.js 22+, PostgreSQL 16+, pg_partman 5.x,
and pg_jsonschema 0.3+ available to the database migration role.

```bash
pip install bursar[postgres]
export DATABASE_URL=postgresql://...
bursar migrate
```

`bursar migrate` applies the ordered SQL baseline and records checksums, so it
is safe to re-run. Hosts can append their own idempotent SQL in the same
transaction with `bursar migrate --post-migrate-sql ./host-integration.sql`.
Pre-release databases must be dropped and recreated; no conversion migration is
supplied.

Create a tenant, then build the facade:

```bash
bursar tenant create acme --id 018f7f5f-7b4a-7000-8000-000000000001
```

```python
from bursar import Bursar, PostgresStore

store = PostgresStore(database_url, tenant_id=tenant_id)
bursar = Bursar.create(credit_store=store)

added = bursar.credits.add_credits(user_id, 1_000, entry_type="purchase")
charged = bursar.credits.deduct_credits(user_id, 25)
entry = bursar.credits.get_ledger_entry(user_id, charged.entry_id)
```

```ts
import { Bursar, PostgresStore } from "@zonastery/bursar";

const bursar = new Bursar({
  creditStore: new PostgresStore(process.env.DATABASE_URL!, tenantId),
});

const added = await bursar.credits.addCredits(userId, 1_000, {
  type: "purchase",
});
const charged = await bursar.credits.deductCredits(userId, 25);
const entry = await bursar.credits.getLedgerEntry(userId, charged.entryId);
```

Pricing is one strict, versioned document. Publish and activate it through the
facade:

```yaml
version: 1
pricing:
  operations:
    completion:
      measures: { input_tokens: { unit: token }, output_tokens: { unit: token } }
      dimensions: { model: { type: string } }
  rate_cards:
    standard:
      operations:
        completion:
          unmatched:
            action: charge
            charge:
              type: sum
              components:
                - { type: per_unit, measure: input_tokens, rate: "0.05" }
                - { type: per_unit, measure: output_tokens, rate: "0.10" }
credits:
  buckets:
    purchased: { priority: 10 }
  default_bucket: purchased
```

```python
bursar.catalog.publish_and_activate(config)
```

```ts
bursar.catalog.publishAndActivate(config);
```

## Documentation

The full documentation — concepts, guides, CLI, and API references — is at
[https://zonastery.github.io/bursar/](https://zonastery.github.io/bursar/).

- [Python package](python/README.md) — `bursar` on PyPI
- [JavaScript package](javascript/README.md) — `@zonastery/bursar` on npm
- [Contributing](CONTRIBUTING.md)

## License

AGPL-3.0. See [LICENSE](LICENSE).
