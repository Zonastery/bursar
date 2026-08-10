-- Tenant-bound outbox claim renewal, acknowledgement, and recovery APIs.
--
-- BursarRuntime uses the operator role for optional external projections, but
-- every repository instance is bound to one transaction-local tenant. These
-- RPCs preserve that boundary inside PostgreSQL before their SECURITY DEFINER
-- privileges can reach the forced-RLS outbox table.

ALTER TABLE bursar.event_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.event_outbox FORCE ROW LEVEL SECURITY;

CREATE INDEX event_outbox_tenant_claimable_idx
ON bursar.event_outbox (
    tenant_id,
    (
        CASE status
            WHEN 'pending' THEN available_at
            WHEN 'processing' THEN claim_expires_at
        END
    ),
    created_at,
    id
)
WHERE status IN ('pending', 'processing');

CREATE INDEX event_outbox_tenant_status_idx
ON bursar.event_outbox (tenant_id, status);

CREATE INDEX event_outbox_tenant_dead_letter_idx
ON bursar.event_outbox (tenant_id, created_at, id)
WHERE status = 'dead_letter';

CREATE OR REPLACE FUNCTION bursar.claim_outbox_events(
    p_tenant_id uuid,
    p_limit integer DEFAULT 100,
    p_lease_seconds integer DEFAULT 60,
    p_topics text [] DEFAULT NULL
)
RETURNS TABLE (
    event_id bigint,
    tenant_id uuid,
    topic text,
    aggregate_type text,
    aggregate_id uuid,
    payload_version smallint,
    payload jsonb,
    claim_token uuid,
    attempt_count integer,
    created_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_token uuid := gen_random_uuid();
BEGIN
    IF p_tenant_id IS NULL
       OR p_tenant_id IS DISTINCT FROM bursar.current_tenant_id()
    THEN
        RAISE EXCEPTION 'outbox tenant context does not match claim request'
            USING ERRCODE = '42501';
    END IF;

    IF p_limit IS NULL
       OR p_lease_seconds IS NULL
       OR p_limit NOT BETWEEN 1 AND 1000
       OR p_lease_seconds NOT BETWEEN 1 AND 3600
       OR (
           p_topics IS NOT NULL
           AND (
               cardinality(p_topics) NOT BETWEEN 1 AND 64
               OR EXISTS (
                   SELECT 1
                   FROM unnest(p_topics) AS requested(topic)
                   WHERE requested.topic IS NULL
                      OR NOT bursar.is_nonempty_text(requested.topic)
                      OR NOT bursar.is_bounded_text(requested.topic, 255)
               )
           )
       )
    THEN
        RAISE EXCEPTION 'invalid tenant outbox claim request'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH claimed AS (
        SELECT outbox.id
        FROM bursar.event_outbox AS outbox
        JOIN bursar.tenants AS tenant
          ON tenant.id = outbox.tenant_id
         AND tenant.status = 'active'
        WHERE outbox.tenant_id = p_tenant_id
          AND outbox.status IN ('pending', 'processing')
          AND CASE outbox.status
              WHEN 'pending' THEN outbox.available_at
              WHEN 'processing' THEN outbox.claim_expires_at
          END <= now()
          AND (p_topics IS NULL OR outbox.topic = ANY(p_topics))
        ORDER BY
            CASE outbox.status
                WHEN 'pending' THEN outbox.available_at
                WHEN 'processing' THEN outbox.claim_expires_at
            END,
            outbox.created_at,
            outbox.id
        FOR UPDATE OF outbox SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE bursar.event_outbox AS outbox
    SET status = 'processing',
        claim_token = v_token,
        claim_expires_at = now() + make_interval(secs => p_lease_seconds),
        attempt_count = outbox.attempt_count + 1,
        last_error = NULL
    FROM claimed
    WHERE outbox.id = claimed.id
      AND outbox.tenant_id = p_tenant_id
    RETURNING
        outbox.id,
        outbox.tenant_id,
        outbox.topic,
        outbox.aggregate_type,
        outbox.aggregate_id,
        outbox.payload_version,
        outbox.payload,
        outbox.claim_token,
        outbox.attempt_count,
        outbox.created_at;
END
$$;

CREATE FUNCTION bursar.renew_tenant_outbox_claim(
    p_tenant_id uuid,
    p_event_id bigint,
    p_claim_token uuid,
    p_lease_seconds integer
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_updated boolean;
BEGIN
    IF p_tenant_id IS NULL
       OR p_tenant_id IS DISTINCT FROM bursar.current_tenant_id()
    THEN
        RAISE EXCEPTION 'outbox tenant context does not match renewal request'
            USING ERRCODE = '42501';
    END IF;

    IF p_event_id IS NULL
       OR p_event_id <= 0
       OR p_claim_token IS NULL
       OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND 3600
    THEN
        RAISE EXCEPTION 'invalid outbox renewal request'
            USING ERRCODE = '22023';
    END IF;

    UPDATE bursar.event_outbox
    SET claim_expires_at = GREATEST(
            claim_expires_at,
            now() + make_interval(secs => p_lease_seconds)
        )
    WHERE tenant_id = p_tenant_id
      AND id = p_event_id
      AND status = 'processing'
      AND claim_token = p_claim_token
      AND claim_expires_at > now()
    RETURNING true INTO v_updated;

    RETURN COALESCE(v_updated, false);
END
$$;

CREATE FUNCTION bursar.complete_tenant_outbox_event(
    p_tenant_id uuid,
    p_event_id bigint,
    p_claim_token uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_updated boolean;
BEGIN
    IF p_tenant_id IS NULL
       OR p_tenant_id IS DISTINCT FROM bursar.current_tenant_id()
    THEN
        RAISE EXCEPTION 'outbox tenant context does not match completion request'
            USING ERRCODE = '42501';
    END IF;

    IF p_event_id IS NULL OR p_event_id <= 0 OR p_claim_token IS NULL THEN
        RAISE EXCEPTION 'invalid outbox completion request'
            USING ERRCODE = '22023';
    END IF;

    UPDATE bursar.event_outbox
    SET status = 'delivered',
        claim_token = NULL,
        claim_expires_at = NULL,
        delivered_at = now()
    WHERE tenant_id = p_tenant_id
      AND id = p_event_id
      AND status = 'processing'
      AND claim_token = p_claim_token
      AND claim_expires_at > now()
    RETURNING true INTO v_updated;

    RETURN COALESCE(v_updated, false);
END
$$;

CREATE FUNCTION bursar.fail_tenant_outbox_event(
    p_tenant_id uuid,
    p_event_id bigint,
    p_claim_token uuid,
    p_error text,
    p_retry_delay_seconds integer DEFAULT 30,
    p_attempt_limit integer DEFAULT 10
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_updated boolean;
BEGIN
    IF p_tenant_id IS NULL
       OR p_tenant_id IS DISTINCT FROM bursar.current_tenant_id()
    THEN
        RAISE EXCEPTION 'outbox tenant context does not match failure request'
            USING ERRCODE = '42501';
    END IF;

    IF p_event_id IS NULL
       OR p_event_id <= 0
       OR p_claim_token IS NULL
       OR p_retry_delay_seconds IS NULL
       OR p_attempt_limit IS NULL
       OR p_retry_delay_seconds NOT BETWEEN 0 AND 86400
       OR p_attempt_limit NOT BETWEEN 1 AND 100
       OR NOT bursar.is_nonempty_bounded_text(p_error, 257)
       OR p_error !~ '^[A-Za-z][A-Za-z0-9_.-]{0,127}:[A-Za-z][A-Za-z0-9_.-]{0,127}$'
    THEN
        RAISE EXCEPTION 'invalid outbox failure request'
            USING ERRCODE = '22023';
    END IF;

    UPDATE bursar.event_outbox
    SET status = CASE
            WHEN attempt_count >= p_attempt_limit THEN 'dead_letter'
            ELSE 'pending'
        END,
        claim_token = NULL,
        claim_expires_at = NULL,
        available_at = now() + make_interval(secs => p_retry_delay_seconds),
        last_error = p_error
    WHERE tenant_id = p_tenant_id
      AND id = p_event_id
      AND status = 'processing'
      AND claim_token = p_claim_token
      AND claim_expires_at > now()
    RETURNING true INTO v_updated;

    RETURN COALESCE(v_updated, false);
END
$$;

CREATE FUNCTION bursar.get_outbox_stats(p_tenant_id uuid)
RETURNS TABLE (
    pending_count bigint,
    processing_count bigint,
    delivered_count bigint,
    dead_letter_count bigint,
    oldest_pending_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    IF p_tenant_id IS NULL
       OR p_tenant_id IS DISTINCT FROM bursar.current_tenant_id()
    THEN
        RAISE EXCEPTION 'outbox tenant context does not match stats request'
            USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT
        count(*) FILTER (WHERE outbox.status = 'pending'),
        count(*) FILTER (WHERE outbox.status = 'processing'),
        count(*) FILTER (WHERE outbox.status = 'delivered'),
        count(*) FILTER (WHERE outbox.status = 'dead_letter'),
        min(outbox.available_at) FILTER (WHERE outbox.status = 'pending')
    FROM bursar.event_outbox AS outbox
    WHERE outbox.tenant_id = p_tenant_id;
END
$$;

CREATE FUNCTION bursar.list_outbox_dead_letters(
    p_tenant_id uuid,
    p_cursor_created_at timestamptz DEFAULT NULL,
    p_cursor_event_id bigint DEFAULT NULL,
    p_limit integer DEFAULT 100
)
RETURNS TABLE (
    event_id bigint,
    tenant_id uuid,
    topic text,
    aggregate_type text,
    aggregate_id uuid,
    payload_version smallint,
    attempt_count integer,
    last_error text,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    IF p_tenant_id IS NULL
       OR p_tenant_id IS DISTINCT FROM bursar.current_tenant_id()
    THEN
        RAISE EXCEPTION 'outbox tenant context does not match dead-letter request'
            USING ERRCODE = '42501';
    END IF;

    IF p_limit IS NULL
       OR p_limit NOT BETWEEN 1 AND 100
       OR (p_cursor_created_at IS NULL) <> (p_cursor_event_id IS NULL)
       OR (p_cursor_event_id IS NOT NULL AND p_cursor_event_id <= 0)
    THEN
        RAISE EXCEPTION 'invalid outbox dead-letter list request'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
        outbox.id,
        outbox.tenant_id,
        outbox.topic,
        outbox.aggregate_type,
        outbox.aggregate_id,
        outbox.payload_version,
        outbox.attempt_count,
        outbox.last_error,
        outbox.created_at,
        outbox.updated_at
    FROM bursar.event_outbox AS outbox
    WHERE outbox.tenant_id = p_tenant_id
      AND outbox.status = 'dead_letter'
      AND (
          p_cursor_created_at IS NULL
          OR (outbox.created_at, outbox.id) > (
              p_cursor_created_at,
              p_cursor_event_id
          )
      )
    ORDER BY outbox.created_at, outbox.id
    LIMIT p_limit + 1;
END
$$;

CREATE FUNCTION bursar.requeue_outbox_dead_letter(
    p_tenant_id uuid,
    p_event_id bigint
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_updated boolean;
BEGIN
    IF p_tenant_id IS NULL
       OR p_tenant_id IS DISTINCT FROM bursar.current_tenant_id()
    THEN
        RAISE EXCEPTION 'outbox tenant context does not match requeue request'
            USING ERRCODE = '42501';
    END IF;

    IF p_event_id IS NULL OR p_event_id <= 0 THEN
        RAISE EXCEPTION 'invalid outbox requeue request'
            USING ERRCODE = '22023';
    END IF;

    UPDATE bursar.event_outbox
    SET status = 'pending',
        attempt_count = 0,
        available_at = now(),
        claim_token = NULL,
        claim_expires_at = NULL,
        last_error = NULL,
        delivered_at = NULL
    WHERE tenant_id = p_tenant_id
      AND id = p_event_id
      AND status = 'dead_letter'
    RETURNING true INTO v_updated;

    RETURN COALESCE(v_updated, false);
END
$$;

REVOKE ALL
ON FUNCTION bursar.renew_tenant_outbox_claim(uuid, bigint, uuid, integer)
FROM PUBLIC;
REVOKE ALL
ON FUNCTION bursar.complete_tenant_outbox_event(uuid, bigint, uuid)
FROM PUBLIC;
REVOKE ALL
ON FUNCTION bursar.fail_tenant_outbox_event(
    uuid, bigint, uuid, text, integer, integer
)
FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.get_outbox_stats(uuid) FROM PUBLIC;
REVOKE ALL
ON FUNCTION bursar.list_outbox_dead_letters(
    uuid, timestamptz, bigint, integer
)
FROM PUBLIC;
REVOKE ALL
ON FUNCTION bursar.requeue_outbox_dead_letter(uuid, bigint)
FROM PUBLIC;

GRANT EXECUTE
ON FUNCTION bursar.renew_tenant_outbox_claim(uuid, bigint, uuid, integer)
TO bursar_operator;
GRANT EXECUTE
ON FUNCTION bursar.complete_tenant_outbox_event(uuid, bigint, uuid)
TO bursar_operator;
GRANT EXECUTE
ON FUNCTION bursar.fail_tenant_outbox_event(
    uuid, bigint, uuid, text, integer, integer
)
TO bursar_operator;
GRANT EXECUTE ON FUNCTION bursar.get_outbox_stats(uuid)
TO bursar_operator;
GRANT EXECUTE
ON FUNCTION bursar.list_outbox_dead_letters(
    uuid, timestamptz, bigint, integer
)
TO bursar_operator;
GRANT EXECUTE
ON FUNCTION bursar.requeue_outbox_dead_letter(uuid, bigint)
TO bursar_operator;

COMMENT ON FUNCTION bursar.renew_tenant_outbox_claim(
    uuid, bigint, uuid, integer
) IS 'Extends one active outbox claim within the caller tenant context.';
COMMENT ON FUNCTION bursar.complete_tenant_outbox_event(uuid, bigint, uuid)
IS 'Acknowledges one delivered outbox event within the caller tenant context.';
COMMENT ON FUNCTION bursar.fail_tenant_outbox_event(
    uuid, bigint, uuid, text, integer, integer
) IS 'Retries or dead-letters one claimed event within the caller tenant context.';
COMMENT ON FUNCTION bursar.get_outbox_stats(uuid)
IS 'Returns aggregate outbox status counts for the caller tenant context.';
COMMENT ON FUNCTION bursar.list_outbox_dead_letters(
    uuid, timestamptz, bigint, integer
) IS 'Lists one bounded keyset page of dead letters for the caller tenant context.';
COMMENT ON FUNCTION bursar.requeue_outbox_dead_letter(uuid, bigint)
IS 'Resets one dead letter for bounded redelivery within the caller tenant context.';
