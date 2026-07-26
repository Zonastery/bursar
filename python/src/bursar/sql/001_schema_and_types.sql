SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET client_min_messages = warning;

COMMENT ON SCHEMA bursar IS
    'Backend-only Bursar accounting, catalog, and billing schema.';

CREATE FUNCTION bursar.is_finite_numeric(p_value numeric)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
$$;

CREATE FUNCTION bursar.current_provider_environment()
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT CASE
        WHEN current_setting('bursar.provider_environment', true)
             IN ('live', 'test', 'sandbox')
        THEN current_setting('bursar.provider_environment', true)
        ELSE 'live'
    END
$$;

CREATE FUNCTION bursar.handle_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
