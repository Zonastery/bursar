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
table; each integration attaches the hook explicitly from its own migration.

## Function conventions

Public RPCs must document their purpose, idempotency key, locking assumptions,
and stable error codes. `SECURITY DEFINER` functions must keep an empty
`search_path` and schema-qualify every object reference. Internal mutation
functions must use the `bursar.mutation_context` trigger contract.

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
