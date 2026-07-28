"""Static and database-level checks for the bundled SQL contract."""

from __future__ import annotations

import re
from pathlib import Path

import psycopg2

from bursar.sql import _get_sql_files

SQL_DIR = Path(__file__).parents[1] / "src" / "bursar" / "sql"


def test_migration_files_are_contiguous_and_self_contained() -> None:
    files = _get_sql_files()
    prefixes = [int(path.stem.split("_", 1)[0]) for path in files]

    assert prefixes == list(range(1, len(files) + 1))
    assert len({path.name for path in files}) == len(files)
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert content.strip(), path
        assert content.endswith("\n"), path


def test_service_role_allowlist_is_granted_and_public_execution_is_revoked(
    pg_database_url: str,
) -> None:
    privilege_sql = (SQL_DIR / "026_privileges.sql").read_text(encoding="utf-8")
    signatures = sorted(set(re.findall(r"'(bursar\.[^']+\([^']*\))'", privilege_sql)))
    assert signatures

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        for signature in signatures:
            cursor.execute(
                "SELECT has_function_privilege('service_role', %s, 'EXECUTE')",
                (signature,),
            )
            assert cursor.fetchone() == (True,), signature

        cursor.execute(
            """
            SELECT p.oid::regprocedure::text
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = 'bursar'
              AND has_function_privilege('public', p.oid, 'EXECUTE')
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT c.relname, r.rolname, privilege_type
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            CROSS JOIN pg_roles AS r
            CROSS JOIN LATERAL (
                SELECT unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS privilege_type
            ) AS privileges
            WHERE n.nspname = 'bursar'
              AND c.relkind IN ('r', 'p')
              AND r.rolname IN ('anon', 'authenticated')
              AND has_table_privilege(r.rolname, c.oid, privileges.privilege_type)
            """
        )
        assert cursor.fetchall() == []


def test_schema_and_public_rpc_comments_are_present(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'bursar'
              AND c.relkind IN ('r', 'p')
              AND c.relname <> 'schema_migrations'
              AND obj_description(c.oid, 'pg_class') IS NULL
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT obj_description(p.oid, 'pg_proc')
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = 'bursar'
              AND p.proname IN (
                  'publish_and_activate_catalog',
                  'provision_subject_account_on_insert',
                  'post_credit',
                  'charge_usage_for_operation',
                  'create_lease_for_operation',
                  'settle_lease',
                  'upsert_auto_recharge_profile',
                  'list_feature_limit_events'
              )
            """
        )
        comments = cursor.fetchall()
        assert comments
        assert all(comment[0] for comment in comments)
