--
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET xmloption = content;
SET client_min_messages = warning;

--
-- Name: bursar; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS bursar;


--
-- Name: SCHEMA bursar; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA bursar IS 'Backend-only Bursar accounting, catalog, and billing schema.';

REVOKE ALL ON SCHEMA bursar FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA bursar TO service_role;

CREATE OR REPLACE FUNCTION bursar.is_finite_numeric(p_value numeric)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
  SELECT p_value IS NOT NULL
     AND p_value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
$$;

REVOKE ALL ON FUNCTION bursar.is_finite_numeric(numeric) FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION bursar.current_provider_environment()
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path TO ''
AS $$
  SELECT CASE WHEN current_setting('bursar.provider_environment', true)
                   IN ('live','test','sandbox')
              THEN current_setting('bursar.provider_environment', true)
              ELSE 'live' END;
$$;
REVOKE ALL ON FUNCTION bursar.current_provider_environment() FROM PUBLIC, anon, authenticated;


-- Bursar owns its timestamp trigger helper.  This keeps the backend schema
-- independent of Supabase's public helper while retaining identical behavior.
CREATE OR REPLACE FUNCTION bursar.handle_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


--
-- Name: credit_tx_type; Type: TYPE; Schema: bursar; Owner: -
--

CREATE TYPE bursar.credit_tx_type AS ENUM (
    'purchase',
    'subscription',
    'signup_bonus',
    'usage',
    'refund',
    'adjustment',
    'team_usage',
    'cycle_grant',
    'cycle_grant_revoke'
);


--
