REVOKE ALL ON FUNCTION bursar.resolve_catalog_plan(text, text, text) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION bursar.resolve_catalog_plan(text, text, text)
            TO service_role;
    END IF;
END
$$;
