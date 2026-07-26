DO $$
DECLARE
    v_table record;
BEGIN
    FOR v_table IN
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname = 'bursar'
          AND tablename <> 'schema_migrations'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
            v_table.schemaname,
            v_table.tablename
        );
        EXECUTE format(
            'CREATE POLICY %I ON %I.%I FOR ALL TO anon, authenticated USING (false) WITH CHECK (false)',
            'backend_only_' || v_table.tablename,
            v_table.schemaname,
            v_table.tablename
        );
    END LOOP;
END;
$$;
