-- Operation lease orchestration RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.create_lease_for_operation(
    p_subject_id uuid,
    p_operation text,
    p_estimate numeric,
    p_idempotency_key text,
    p_feature text DEFAULT NULL,
    p_reserved_calls integer DEFAULT 1,
    p_ttl interval DEFAULT interval '10 minutes',
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
    lease_id uuid,
    status bursar.lease_status,
    reserved_amount numeric,
    reserved_calls integer,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_account uuid;

    v_assignment record;

    v_policy jsonb;

    v_start timestamptz;

    v_end timestamptz;

    v_limit integer;

    v_reserved_calls integer := 0;

    v_result record;

BEGIN
    IF p_feature IS NOT NULL AND p_reserved_calls < 1 THEN
        RETURN QUERY SELECT NULL::uuid,'active'::bursar.lease_status,0::numeric,0,'invalid_request';
 RETURN;

    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    IF p_feature IS NOT NULL THEN
        SELECT a.starts_at,p.limits INTO v_assignment
        FROM bursar.account_plan_assignments a
        JOIN bursar.catalog_plans p ON p.id=a.plan_id AND p.catalog_revision_id=a.catalog_revision_id
        WHERE a.account_id=v_account AND a.starts_at<=now() AND (a.ends_at IS NULL OR a.ends_at>now());

        IF FOUND THEN
            v_policy := COALESCE(v_assignment.limits->p_feature, '{}'::jsonb);

            v_limit := NULLIF(v_policy->>'max_calls','')::integer;

            IF v_limit IS NOT NULL THEN
                v_reserved_calls := COALESCE(p_reserved_calls,1);

                SELECT window_start,window_end INTO v_start,v_end FROM bursar.policy_period_window(
                    v_assignment.starts_at, v_policy #>> '{period,unit}',
                    COALESCE((v_policy #>> '{period,count}')::integer,1),
                    COALESCE(v_policy #>> '{period,anchor}','calendar'),
                    COALESCE(v_policy #>> '{period,timezone}','UTC')
                );

                INSERT INTO bursar.feature_call_windows(account_id,feature,window_start,window_end,limit_value)
                VALUES(v_account,p_feature,v_start,v_end,v_limit)
                ON CONFLICT (account_id,feature,window_start) DO NOTHING;

            END IF;

        END IF;

    END IF;

    SELECT * INTO v_result FROM bursar.create_lease(
        p_subject_id,p_operation,p_estimate,p_idempotency_key,p_feature,
        v_reserved_calls,NULL,p_ttl,'{}'::jsonb,p_metadata
    );

    RETURN QUERY SELECT v_result.lease_id,v_result.status,v_result.reserved_amount,v_result.reserved_calls,v_result.error_code;

END $$;
