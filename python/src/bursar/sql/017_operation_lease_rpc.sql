-- Plan-aware public operation lease.

CREATE FUNCTION bursar.create_lease_for_operation(
    p_subject_id uuid,
    p_operation text,
    p_estimate numeric,
    p_idempotency_key text,
    p_ttl interval DEFAULT interval '10 minutes',
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_feature text DEFAULT NULL,
    p_measures jsonb DEFAULT '{}'::jsonb,
    p_dimensions jsonb DEFAULT '{}'::jsonb,
    p_minimum_balance numeric DEFAULT NULL,
    p_max_concurrent integer DEFAULT NULL
)
RETURNS TABLE (
    lease_id uuid,
    status bursar.lease_status,
    reserved_amount numeric,
    error_code text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT *
    FROM bursar.create_lease(
        p_subject_id,
        p_operation,
        p_estimate,
        p_idempotency_key,
        p_minimum_balance,
        p_ttl,
        '{}'::jsonb,
        p_metadata,
        p_feature,
        p_measures,
        p_dimensions,
        p_max_concurrent
    )
$$;
