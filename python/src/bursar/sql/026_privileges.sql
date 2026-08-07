-- Establish a fail-closed baseline before the dedicated Bursar roles and
-- tenant policies are installed by the final security migration.

REVOKE ALL ON SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL TABLES IN SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bursar FROM PUBLIC;

DO $$
DECLARE
    t record;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables WHERE schemaname='bursar'
    LOOP
        EXECUTE format(
            'ALTER TABLE bursar.%I ENABLE ROW LEVEL SECURITY',
            t.tablename
        );
    END LOOP;
END $$;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON TABLES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON SEQUENCES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON FUNCTIONS FROM PUBLIC;
