# Bursar SQL layout

These files are the canonical database contract shared by the Python and
JavaScript SDKs. The migration runner applies every `NNN_*.sql` file in numeric
order and records its SHA-256 checksum in `bursar.schema_migrations`.

## File boundaries

Files are organised by dependency and domain:

1. bootstrap, types, and shared helpers
2. tables and relational constraints
3. trigger functions and trigger declarations
4. indexes
5. catalog, credit, policy/lease, team, billing, record, query, and plan RPCs
6. privileges and RLS
7. schema and public-RPC comments

Keep a file executable on its own through `psycopg`/`psql`; do not use psql
meta-commands such as `\\ir`. Keep functions in cohesive domain groups rather
than creating one migration per function.

`bursar.provision_subject_account_on_insert()` is an optional host-table
trigger hook. Bursar never guesses or mutates an application-owned principal
table; each integration attaches a host-owned wrapper explicitly from its own
migration. The wrapper must bind `bursar.tenant_id` transaction-locally before
it calls a Bursar provisioning RPC.

## Tenant boundary

Shared-table multi-tenancy is defined directly by the baseline table,
constraint, index, and storage migrations. Business tables have a mandatory
`tenant_id` plus tenant-prefixed relationship and uniqueness constraints.
`031_multitenancy_security.sql` installs forced RLS and assigns tenant RPCs to
the `NOLOGIN NOBYPASSRLS` `bursar_runtime` role, so a Supabase `service_role`
caller cannot bypass isolation through a security-definer RPC.

Server adapters bind `bursar.tenant_id` with transaction-local `set_config` on
the same checked-out connection as the RPC. Trusted PostgREST callers may use
`app_metadata.tenant_id`; `user_metadata` is never accepted. Storage
maintenance and outbox claiming remain operator-level cross-tenant operations,
and their exports carry the owning tenant UUID.

## Function conventions

Public RPCs must document their purpose, idempotency key, locking assumptions,
and stable error codes. `SECURITY DEFINER` functions must keep an empty
`search_path` and schema-qualify every object reference. Internal mutation
functions must use the `bursar.mutation_context` trigger contract.

Externally meaningful, independently generated, and cross-system identifiers
use RFC 9562 UUIDv7. This preserves the UUID API while giving primary and
unique B-tree indexes time-local insert patterns. Internal-only append and
history rows use `bigint GENERATED ALWAYS AS IDENTITY` for narrower primary
and foreign-key indexes. Relationship, singleton, and aggregate tables use
natural or composite keys when those keys are stable. Opaque claim tokens
remain UUIDv4 because they are credentials, not row keys.

Business and provider keys are stored as `text`, trimmed/non-empty where
required, and bounded to 255 characters before they can reach an index.
Currency codes use `text` plus an uppercase ISO-4217-shape check rather than
blank-padded `char(3)`. Closed internal state machines use enums; provider
states and workflows expected to evolve use `text` with a named allowlist
check. Provider-owned records and uniqueness rules must include
`provider_environment`; subject-scoped billing state uses
`(subject_id, provider_environment)`.

Every foreign key must have an index whose leading columns match the child key.
Do not add an index already covered by the leading columns and predicate of
another index. Catalog regression tests enforce both rules.

## Change policy

This repository is still pre-production, so the numbered files may be
reorganised as a new greenfield baseline. Once deployed, migration files are
immutable: formatting or logic changes must be appended as a new migration
because the runner rejects checksum changes.

## Checks

From the Bursar package root:

```bash
gmake test-integration
uv run --with sqlfluff sqlfluff lint python/src/bursar/sql --dialect postgres
```

The SQL regression files in `python/tests/sql*.sql` must pass against a clean
Postgres 16 instance after every baseline change.

## PostgreSQL-first storage lifecycle

Bursar keeps accounting, idempotency, current quota state, and billing claim
state in PostgreSQL permanently. High-cardinality or opaque payloads are kept
in bounded tables:

- `credit_usage_charges` retains the compact accounting receipt and idempotency
  evidence. PostgreSQL mode also stores dimensions/metadata in
  `usage_charge_payloads` and maintains `usage_daily_rollups`; ClickHouse mode
  carries those details in the transactional outbox instead.
- `billing_event_payloads` stores raw provider webhook envelopes when
  PostgreSQL owns payload storage. S3 mode keeps the envelope in the
  transactional outbox until archival succeeds.
- `event_outbox` provides asynchronous delivery to optional external sinks.
- `usage_daily_rollups` serves exact built-in analytics without extra
  infrastructure. Each logical aggregate is deterministically spread across
  32 rows; readers sum those shards, avoiding a single hot row during
  concurrent ingestion.

`bursar.storage_settings` contains retention and quota-lateness policy.
`bursar.run_storage_maintenance()` performs one bounded row-cleanup pass:

- every non-partitioned retention class is capped by
  `maintenance_batch_size`;
- expiry scans use time-leading indexes and `SKIP LOCKED`;
- an advisory lock prevents concurrent maintenance passes; and
- there is no partition DDL in the row-cleanup transaction.

`bursar.run_storage_partition_maintenance()` is a separate, short transaction
for one payload table. It creates the current and next monthly partitions and
drops fully expired partitions with a short lock timeout. A whole-partition
drop avoids row-delete WAL and dead-table bloat. Keeping it separate is
important because PostgreSQL holds DDL locks until transaction commit.

This keeps retention work out of ingestion transactions. Do not call
maintenance inline from an end-user request.

### Scheduling

`pg_cron` is the preferred scheduler when the PostgreSQL provider supports it.
It is deliberately not installed by the baseline because many plain and
managed PostgreSQL installations do not expose the extension. After enabling
it for the database, schedule the inexpensive due-check every minute:

```sql
SELECT cron.schedule(
    'bursar-storage-maintenance',
    '* * * * *',
    'SELECT bursar.maybe_run_storage_maintenance();'
);

SELECT cron.schedule(
    'bursar-usage-partition-maintenance',
    '17 3 * * *',
    $$SELECT bursar.run_storage_partition_maintenance(
        'usage_charge_payloads'
    );$$
);

SELECT cron.schedule(
    'bursar-billing-partition-maintenance',
    '19 3 * * *',
    $$SELECT bursar.run_storage_partition_maintenance(
        'billing_event_payloads'
    );$$
);
```

`maybe_run_storage_maintenance()` observes
`maintenance_interval_seconds`; the default is 60 seconds. The work defaults
to 500 rows per retention class and at most two full partition drops per pass.
Both limits, and the 100 ms partition-DDL lock timeout, are configurable with
`bursar.configure_storage()`.

Without `pg_cron`, run the due-check frequently and both partition-maintenance
calls daily from a single application background timer or an existing worker.
Each call must use its own transaction. This requires no additional service
and remains decoupled from request transactions. Advisory locks make duplicate
schedulers safe, but a dedicated scheduler avoids unnecessary connections.

Rolling quota publication is rejected when configured event retention is
shorter than the quota window plus accepted lateness, correction, and safety
horizons.
