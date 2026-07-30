CREATE SCHEMA IF NOT EXISTS bursar;

CREATE SCHEMA IF NOT EXISTS extensions;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

SET client_encoding = 'UTF8';

SET standard_conforming_strings = on;

SET client_min_messages = warning;

COMMENT ON SCHEMA bursar IS
'Backend-only Bursar accounting, catalog, and billing schema.';

CREATE FUNCTION bursar.uuid_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
SET search_path TO ''
AS $$
DECLARE
    v_unix_ms bigint := floor(
        extract(epoch FROM clock_timestamp()) * 1000
    );
    v_bytes bytea := extensions.gen_random_bytes(16);
BEGIN
    -- RFC 9562 UUIDv7: 48-bit Unix millisecond timestamp, version 7,
    -- RFC 4122 variant, and 74 random bits. Values remain ordinary UUIDs
    -- while newly inserted B-tree keys have time locality.
    v_bytes := set_byte(v_bytes, 0, ((v_unix_ms >> 40) & 255)::integer);
    v_bytes := set_byte(v_bytes, 1, ((v_unix_ms >> 32) & 255)::integer);
    v_bytes := set_byte(v_bytes, 2, ((v_unix_ms >> 24) & 255)::integer);
    v_bytes := set_byte(v_bytes, 3, ((v_unix_ms >> 16) & 255)::integer);
    v_bytes := set_byte(v_bytes, 4, ((v_unix_ms >> 8) & 255)::integer);
    v_bytes := set_byte(v_bytes, 5, (v_unix_ms & 255)::integer);
    v_bytes := set_byte(
        v_bytes,
        6,
        (get_byte(v_bytes, 6) & 15) | 112
    );
    v_bytes := set_byte(
        v_bytes,
        8,
        (get_byte(v_bytes, 8) & 63) | 128
    );

    RETURN encode(v_bytes, 'hex')::uuid;
END
$$;

CREATE FUNCTION bursar.is_finite_numeric(
    p_value numeric
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
$$;

CREATE FUNCTION bursar.digest_numeric_text(
    p_value numeric
) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path TO '' AS $$
    SELECT CASE
        WHEN p_value IS NULL THEN NULL
        WHEN p_value = trunc(p_value) THEN trunc(p_value)::text
        ELSE rtrim(rtrim(p_value::text,'0'),'.')
    END
$$;

CREATE FUNCTION bursar.require_json_object(
    p_value jsonb,
    p_name text
) RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
SET search_path TO ''
AS $$
BEGIN
    IF p_value IS NULL OR jsonb_typeof(p_value) <> 'object' THEN
        RAISE EXCEPTION '% must be a JSON object', p_name
            USING ERRCODE = '22023';
    END IF;

    RETURN p_value;
END
$$;

CREATE FUNCTION bursar.is_bounded_json_object(
    p_value jsonb,
    p_max_bytes integer
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_max_bytes > 0
       AND jsonb_typeof(p_value) = 'object'
       AND octet_length(p_value::text) <= p_max_bytes
$$;

CREATE FUNCTION bursar.is_bounded_text(
    p_value text,
    p_max_characters integer
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_max_characters > 0
       AND length(p_value) <= p_max_characters
$$;

CREATE FUNCTION bursar.policy_duration_seconds(
    p_policy jsonb
) RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
DECLARE
    v_unit text;
    v_count bigint;
    v_unit_seconds bigint;
BEGIN
    IF p_policy IS NULL
       OR jsonb_typeof(p_policy) <> 'object'
       OR p_policy->>'type' <> 'rolling'
    THEN
        RETURN 0;
    END IF;

    v_unit := p_policy #>> '{duration,unit}';
    v_count := (p_policy #>> '{duration,count}')::bigint;

    IF v_count IS NULL OR v_count < 1 THEN
        RETURN NULL;
    END IF;

    v_unit_seconds := CASE v_unit
        WHEN 'second' THEN 1
        WHEN 'minute' THEN 60
        WHEN 'hour' THEN 3600
        WHEN 'day' THEN 86400
        WHEN 'week' THEN 604800
        -- Retention validation deliberately uses conservative upper bounds.
        WHEN 'month' THEN 2678400
        WHEN 'year' THEN 31622400
        ELSE NULL
    END;

    IF v_unit_seconds IS NULL
       OR v_count > 9223372036854775807 / v_unit_seconds
    THEN
        RETURN NULL;
    END IF;

    RETURN v_count * v_unit_seconds;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN NULL;
END
$$;

CREATE FUNCTION bursar.is_nonempty_text(
    p_value text
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path TO ''
AS $$
    SELECT p_value IS NOT NULL
       AND p_value = btrim(p_value)
       AND length(btrim(p_value)) BETWEEN 1 AND 255
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
