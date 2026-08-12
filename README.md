# Bursar — Open-source AI credits and usage billing

[![CI](https://github.com/Zonastery/bursar/actions/workflows/ci.yml/badge.svg)](https://github.com/Zonastery/bursar/actions/workflows/ci.yml)
[![Docs](https://github.com/Zonastery/bursar/actions/workflows/docs.yml/badge.svg)](https://github.com/Zonastery/bursar/actions/workflows/docs.yml)
[![PyPI](https://img.shields.io/pypi/v/bursar.svg)](https://pypi.org/project/bursar/)
[![PyPI downloads](https://img.shields.io/pypi/dm/bursar.svg)](https://pypi.org/project/bursar/)
[![npm](https://img.shields.io/npm/v/@zonastery/bursar.svg)](https://www.npmjs.com/package/@zonastery/bursar)
[![npm downloads](https://img.shields.io/npm/dm/@zonastery/bursar.svg)](https://www.npmjs.com/package/@zonastery/bursar)
[![Go Reference](https://pkg.go.dev/badge/github.com/Zonastery/bursar/golang/v2.svg)](https://pkg.go.dev/github.com/Zonastery/bursar/golang/v2)
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
document. The Python, TypeScript, and Go SDKs share both, so they produce
identical accounting and identical bills.

## Highlights

- **Canonical accounting** — `credit_accounts.balance` is the only stored
  account balance and `credit_ledger_entries` the only monetary history;
  per-bucket-lot spend counters in `credit_lots` are the only derived
  projection, so there are no projected transaction or bucket-balance tables.
- **Declarative configuration** — operations, rate cards, plans, allowances,
  and billing live in one strict, versioned document, published through the
  SDK and readable by billing and auto-recharge.
- **Financial safety by default** — reserve-then-settle leases with
  idempotency keys, expiry, and strict-prepaid or overdraft policies.
- **Safe expressions** — an AST-based evaluator with a strict allowlist: no
  `eval`, no arbitrary code execution.
- **Identical behavior in Python, JavaScript, and Go** — same config, same
  rounding, same results.

## Quick start

Requirements: Python 3.12 or 3.13, Node.js 22+, or Go 1.25+; PostgreSQL 16+,
pg_partman 5.x, and pg_jsonschema 0.3+ available to the database migration role.

```bash
python -m pip install "bursar[postgres]"
export BURSAR_MIGRATION_DATABASE_URL=postgresql://bursar_migrator@db.example.com/bursar
bursar migrate
```

`bursar migrate` applies the ordered SQL baseline and records checksums, so it
is safe to re-run. Run it as a dedicated migration principal, not with the
application's runtime credentials.

Go applications use the same Python-owned migration CLI and install the
versioned SDK module separately:

```bash
go get github.com/Zonastery/bursar/golang/v2
```

Create a tenant, then build the facade:

```bash
export BURSAR_OPERATOR_DATABASE_URL=postgresql://bursar_ops@db.example.com/bursar
bursar tenant create acme --id 018f7f5f-7b4a-7000-8000-000000000001
export DATABASE_URL=postgresql://bursar_app@db.example.com/bursar
export BURSAR_TENANT_ID=018f7f5f-7b4a-7000-8000-000000000001
```

Provision `bursar_ops` and `bursar_app` as separate SET-only members of
`bursar_operator` and `bursar_client`, respectively. The
[CLI guide](https://zonastery.github.io/bursar/docs/cli) has the exact SQL and
caller-role contract; never use migration-owner or `BYPASSRLS` credentials in
the application.

```python
import os
from decimal import Decimal

from bursar import Bursar, PostgresStore

store = PostgresStore(
    os.environ["DATABASE_URL"],
    tenant_id=os.environ["BURSAR_TENANT_ID"],
    provider_environment="test",
)
bursar = Bursar(credit_store=store)

added = bursar.credits.add_credits(
    user_id,
    Decimal("1000"),
    entry_type="purchase",
    idempotency_key="checkout:order-42",
)
charged = bursar.credits.deduct_credits(
    user_id,
    Decimal("25"),
    idempotency_key="request:job-42",
)
entry = bursar.credits.get_ledger_entry(user_id, charged.entry_id)
```

```ts
import { Bursar, PostgresStore } from "@zonastery/bursar";

const bursar = new Bursar({
  creditStore: new PostgresStore({
    postgres: process.env.DATABASE_URL!,
    tenantId,
    providerEnvironment: "test",
  }),
});

const added = await bursar.credits.addCredits(userId, "1000", {
  type: "purchase",
  idempotencyKey: "checkout:order-42",
});
const charged = await bursar.credits.deductCredits(userId, "25", {
  idempotencyKey: "request:job-42",
});
const entry = await bursar.credits.getLedgerEntry(userId, charged.entryId);
```

```go
package main

import (
	"context"
	"log"
	"os"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

func main() {
	ctx := context.Background()
	store, err := bursar.NewPostgresStore(ctx, os.Getenv("DATABASE_URL"), bursar.PostgresStoreOptions{
		TenantID:            os.Getenv("BURSAR_TENANT_ID"),
		ProviderEnvironment: bursar.ProviderEnvironmentTest,
	})
	if err != nil {
		log.Fatal(err)
	}
	defer store.Close()

	sdk, err := bursar.New(bursar.Options{CreditStore: store})
	if err != nil {
		log.Fatal(err)
	}
	if err := sdk.LoadCatalog(ctx); err != nil {
		log.Fatal(err)
	}
}
```

Pricing is one strict, versioned document. Publish and activate it through the
facade:

```yaml
version: 1
pricing:
  operations:
    completion:
      measures:
        { input_tokens: { unit: token }, output_tokens: { unit: token } }
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
await bursar.catalog.publishAndActivate(config);
```

## Documentation

The full documentation — concepts, guides, CLI, and API references — is at
[https://zonastery.github.io/bursar/](https://zonastery.github.io/bursar/).

- [Python package](python/README.md) — `bursar` on PyPI
- [TypeScript and JavaScript package](javascript/README.md) — `@zonastery/bursar` on npm
- [Go package](docs/docs/go-api/index.mdx) — `github.com/Zonastery/bursar/golang/v2` on pkg.go.dev
- [Build a prepaid credit system for AI SaaS](https://zonastery.github.io/bursar/docs/guides/ai-saas-credits)
- [Changelog](CHANGELOG.md)
- [Citation metadata](CITATION.cff)
- [Contributing](CONTRIBUTING.md)

## Agent-readable documentation

Bursar publishes the same maintained documentation in formats designed for
coding agents and retrieval systems:

- [`llms.txt`](https://zonastery.github.io/bursar/llms.txt) — ordered map of the canonical documentation
- [`llms-full.txt`](https://zonastery.github.io/bursar/llms-full.txt) — complete documentation corpus
- [Bursar Agent Skill](https://zonastery.github.io/bursar/docs/agent-skills) — install with `npx skills add zonastery/bursar@bursar`
- [Context7](https://context7.com/zonastery/bursar) and [DeepWiki](https://deepwiki.com/Zonastery/bursar) — external indexes

When an indexed answer and an installed package disagree, use the documentation
for the installed version or the matching release tag as the source of truth.

## License

AGPL-3.0. See [LICENSE](LICENSE).
