-- Reset every Bursar-owned data table while preserving the migration ledger.
-- Both SDK integration harnesses execute this file between tests so cleanup
-- cannot drift as new tables are added to the baseline.
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

-- The singleton is part of the installed baseline, not test-owned state.
INSERT INTO bursar.storage_settings(singleton)
VALUES (TRUE);
