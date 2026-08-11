-- Reset every Bursar-owned data table while preserving the migration ledger.
-- Both SDK integration harnesses execute this file between tests so cleanup
-- cannot drift as new tables are added to the baseline.
--
-- Partition payload tables and their children are deliberately owned by the
-- NOLOGIN partition runtime. Temporarily lend the migration session TRUNCATE
-- on only that ownership domain, then revoke it in the same reset command.
SET ROLE bursar_partition_runtime;
DO $grant_partition_reset$
DECLARE
    v_tables text;
BEGIN
    SELECT string_agg(
        format('%I.%I', namespace_info.nspname, table_info.relname),
        ', ' ORDER BY table_info.relname
    )
    INTO v_tables
    FROM pg_class AS table_info
    JOIN pg_namespace AS namespace_info
      ON namespace_info.oid = table_info.relnamespace
    WHERE namespace_info.nspname = 'bursar'
      AND table_info.relkind IN ('r', 'p')
      AND table_info.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user);

    IF v_tables IS NOT NULL THEN
        EXECUTE format(
            'GRANT TRUNCATE ON TABLE %s TO %I',
            v_tables,
            session_user
        );
    END IF;
END
$grant_partition_reset$;
RESET ROLE;

DO $reset_bursar$
DECLARE
    v_tables text;
BEGIN
    SELECT string_agg(
        format('%I.%I', namespace_info.nspname, table_info.relname),
        ', ' ORDER BY table_info.relname
    )
    INTO v_tables
    FROM pg_class AS table_info
    JOIN pg_namespace AS namespace_info
      ON namespace_info.oid = table_info.relnamespace
    WHERE namespace_info.nspname = 'bursar'
      AND table_info.relkind IN ('r', 'p')
      AND NOT table_info.relispartition
      AND table_info.relname <> 'schema_migrations';

    IF v_tables IS NOT NULL THEN
        EXECUTE 'TRUNCATE TABLE '
            || v_tables
            || ' RESTART IDENTITY CASCADE';
    END IF;
END
$reset_bursar$;

SET ROLE bursar_partition_runtime;
DO $revoke_partition_reset$
DECLARE
    v_tables text;
BEGIN
    SELECT string_agg(
        format('%I.%I', namespace_info.nspname, table_info.relname),
        ', ' ORDER BY table_info.relname
    )
    INTO v_tables
    FROM pg_class AS table_info
    JOIN pg_namespace AS namespace_info
      ON namespace_info.oid = table_info.relnamespace
    WHERE namespace_info.nspname = 'bursar'
      AND table_info.relkind IN ('r', 'p')
      AND table_info.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user);

    IF v_tables IS NOT NULL THEN
        EXECUTE format(
            'REVOKE TRUNCATE ON TABLE %s FROM %I',
            v_tables,
            session_user
        );
    END IF;
END
$revoke_partition_reset$;
RESET ROLE;

-- The singleton is part of the installed baseline, not test-owned state.
INSERT INTO bursar.storage_settings(singleton)
VALUES (TRUE);
