-- Bind checkout intents to the caller's operation key.
--
-- The provider already receives this key as its idempotency key. Persisting the
-- same key locally makes a changed replay return the original intent so the
-- SDK can reject its mismatched request digest before a second provider call.

ALTER TABLE bursar.billing_checkout_intents
ADD COLUMN operation_key text;

-- Forced RLS deliberately excludes the migration role. Temporarily disable it
-- while the table is locked by this transactional migration, then restore the
-- exact fail-closed posture installed by migration 029.
ALTER TABLE bursar.billing_checkout_intents DISABLE ROW LEVEL SECURITY;

UPDATE bursar.billing_checkout_intents
SET operation_key = 'legacy:' || id::text;

ALTER TABLE bursar.billing_checkout_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.billing_checkout_intents FORCE ROW LEVEL SECURITY;

ALTER TABLE bursar.billing_checkout_intents
ALTER COLUMN operation_key SET NOT NULL;

ALTER TABLE bursar.billing_checkout_intents
ADD CONSTRAINT billing_checkout_intents_operation_key_check
CHECK (bursar.is_nonempty_bounded_text(operation_key, 255));

ALTER TABLE bursar.billing_checkout_intents
DROP CONSTRAINT billing_checkout_intents_tenant_id_subject_id_provider_prov_key;

ALTER TABLE bursar.billing_checkout_intents
ADD CONSTRAINT billing_checkout_intents_operation_key_unique
UNIQUE (
    tenant_id,
    subject_id,
    provider,
    provider_environment,
    operation_key
);

-- Migration 029 transferred the old RPC to the non-BYPASSRLS runtime owner.
-- Assume that role only while dropping the owned function; the migration role
-- creates the replacement and then transfers ownership back below.
SET LOCAL ROLE bursar_runtime;

DROP FUNCTION bursar.create_checkout_intent(
    uuid,
    text,
    text,
    text,
    bytea,
    timestamptz,
    text,
    text,
    text
);

RESET ROLE;

CREATE FUNCTION bursar.create_checkout_intent(
    p_subject_id uuid,
    p_provider text,
    p_operation_key text,
    p_checkout_kind text,
    p_product_key text,
    p_request_digest bytea,
    p_expires_at timestamptz,
    p_provider_session_id text DEFAULT NULL,
    p_checkout_url text DEFAULT NULL,
    p_region text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id uuid;
    v_revision uuid;
    v_environment text := bursar.current_provider_environment();
    v_availability jsonb;
    v_object_type text;
BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_nonempty_text(p_provider)
       OR NOT bursar.is_nonempty_bounded_text(p_operation_key, 255)
       OR p_checkout_kind IS NULL
       OR p_checkout_kind NOT IN ('subscription', 'credit_topup')
       OR NOT bursar.is_nonempty_text(p_product_key)
       OR p_request_digest IS NULL
       OR octet_length(p_request_digest) <> 32
       OR p_expires_at IS NULL
       OR p_expires_at <= now()
       OR (
           p_provider_session_id IS NOT NULL
           AND NOT bursar.is_nonempty_text(p_provider_session_id)
       )
       OR (
           p_checkout_url IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_checkout_url, 8192)
       )
       OR (
           p_region IS NOT NULL
           AND upper(p_region) !~ '^[A-Z]{2,3}$'
       )
    THEN
        RAISE EXCEPTION 'invalid checkout intent'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bursar.subjects(id)
    VALUES (p_subject_id)
    ON CONFLICT (tenant_id, id) DO NOTHING;

    IF bursar.is_subject_pseudonymized(p_subject_id) THEN
        RAISE EXCEPTION 'pseudonymized subject cannot create checkout'
            USING ERRCODE = '55000';
    END IF;

    SELECT id
    INTO v_revision
    FROM bursar.catalog_revisions
    WHERE status = 'active';

    IF v_revision IS NULL THEN
        RAISE EXCEPTION 'active catalog missing'
            USING ERRCODE = '23503';
    END IF;

    v_object_type := CASE p_checkout_kind
        WHEN 'subscription' THEN 'offer'
        ELSE 'topup'
    END;

    IF p_checkout_kind = 'subscription' THEN
        SELECT offer.availability
        INTO v_availability
        FROM bursar.catalog_offers AS offer
        WHERE offer.catalog_revision_id = v_revision
          AND offer.offer_key = p_product_key;
    ELSE
        SELECT topup.availability
        INTO v_availability
        FROM bursar.catalog_topups AS topup
        WHERE topup.catalog_revision_id = v_revision
          AND topup.topup_key = p_product_key;
    END IF;

    IF NOT FOUND
       OR NOT EXISTS (
           SELECT 1
           FROM bursar.catalog_provider_refs AS reference
           WHERE reference.catalog_revision_id = v_revision
             AND reference.provider = p_provider
             AND reference.provider_environment = v_environment
             AND reference.object_type = v_object_type
             AND reference.object_key = p_product_key
       )
    THEN
        RAISE EXCEPTION 'checkout product is not available from provider'
            USING ERRCODE = '22023';
    END IF;

    IF (
        v_availability->>'starts_at' IS NOT NULL
        AND (v_availability->>'starts_at')::timestamptz > now()
    ) OR (
        v_availability->>'ends_at' IS NOT NULL
        AND (v_availability->>'ends_at')::timestamptz <= now()
    ) OR (
        jsonb_array_length(
            COALESCE(v_availability->'regions', '[]'::jsonb)
        ) > 0
        AND (
            p_region IS NULL
            OR NOT (v_availability->'regions' ? upper(p_region))
        )
    )
    THEN
        RAISE EXCEPTION 'checkout product is outside its availability'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bursar.billing_checkout_intents AS intent(
        subject_id,
        provider,
        provider_environment,
        operation_key,
        checkout_kind,
        product_key,
        region,
        catalog_revision_id,
        request_digest,
        expires_at,
        provider_session_id,
        checkout_url
    )
    VALUES (
        p_subject_id,
        p_provider,
        v_environment,
        p_operation_key,
        p_checkout_kind,
        p_product_key,
        upper(p_region),
        v_revision,
        p_request_digest,
        p_expires_at,
        p_provider_session_id,
        p_checkout_url
    )
    ON CONFLICT (
        tenant_id,
        subject_id,
        provider,
        provider_environment,
        operation_key
    )
    DO UPDATE SET
        -- Return the existing row without reopening a terminal operation or
        -- rewriting its request. The SDK compares request_digest before any
        -- provider side effect.
        operation_key = intent.operation_key
    RETURNING id INTO v_id;

    RETURN v_id;
END
$$;

REVOKE ALL
ON FUNCTION bursar.create_checkout_intent(
    uuid,
    text,
    text,
    text,
    text,
    bytea,
    timestamptz,
    text,
    text,
    text
)
FROM PUBLIC;

-- PostgreSQL requires the new function owner to have CREATE on the containing
-- schema. Grant that capability only for the ownership transfer and the
-- runtime-owned replacement RPC definitions below; revoke it before commit.
GRANT CREATE ON SCHEMA bursar TO bursar_runtime;

ALTER FUNCTION bursar.create_checkout_intent(
    uuid,
    text,
    text,
    text,
    text,
    bytea,
    timestamptz,
    text,
    text,
    text
)
OWNER TO bursar_runtime;

SET LOCAL ROLE bursar_runtime;

GRANT EXECUTE
ON FUNCTION bursar.create_checkout_intent(
    uuid,
    text,
    text,
    text,
    text,
    bytea,
    timestamptz,
    text,
    text,
    text
)
TO bursar_client;

COMMENT ON FUNCTION bursar.create_checkout_intent(
    uuid,
    text,
    text,
    text,
    text,
    bytea,
    timestamptz,
    text,
    text,
    text
)
IS 'Creates or returns a checkout intent bound to one caller operation key and request digest.';

CREATE OR REPLACE FUNCTION bursar.advance_checkout_intent(
    p_intent_id uuid,
    p_status text DEFAULT NULL,
    p_provider_session_id text DEFAULT NULL,
    p_checkout_url text DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_intent bursar.billing_checkout_intents;
BEGIN
    IF (
           p_status IS NOT NULL
           AND p_status NOT IN ('open', 'completed', 'failed', 'expired')
       )
       OR (
           p_provider_session_id IS NOT NULL
           AND NOT bursar.is_nonempty_text(p_provider_session_id)
       )
       OR (
           p_checkout_url IS NOT NULL
           AND NOT bursar.is_nonempty_bounded_text(p_checkout_url, 8192)
       )
    THEN
        RETURN false;
    END IF;

    SELECT *
    INTO v_intent
    FROM bursar.billing_checkout_intents
    WHERE id = p_intent_id
      AND provider_environment = bursar.current_provider_environment()
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF v_intent.status <> 'open'
       AND (
           p_status IS NULL
           OR p_status <> v_intent.status
           OR p_provider_session_id IS NOT NULL
           OR p_checkout_url IS NOT NULL
       )
    THEN
        RETURN false;
    END IF;

    UPDATE bursar.billing_checkout_intents
    SET status = COALESCE(p_status, status),
        provider_session_id = COALESCE(
            CASE
                WHEN bursar.is_subject_pseudonymized(v_intent.subject_id) THEN NULL
                ELSE p_provider_session_id
            END,
            provider_session_id
        ),
        checkout_url = CASE
            WHEN bursar.is_subject_pseudonymized(v_intent.subject_id) THEN NULL
            ELSE COALESCE(p_checkout_url, checkout_url)
        END,
        updated_at = now()
    WHERE id = p_intent_id;

    RETURN true;
END
$$;

COMMENT ON FUNCTION bursar.advance_checkout_intent(uuid, text, text, text)
IS 'Advances an open checkout intent and rejects data attachment to terminal intents.';

RESET ROLE;

REVOKE CREATE ON SCHEMA bursar FROM bursar_runtime;
