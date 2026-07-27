-- Operation charge orchestration RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.charge_usage_for_operation(
    p_subject_id uuid,
    p_operation text,
    p_requested numeric,
    p_idempotency_key text,
    p_feature text DEFAULT NULL,
    p_model text DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
    charge_id uuid,
    ledger_entry_id uuid,
    charged numeric,
    allowance_covered numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE
    v_account uuid;

    v_assignment record;

    v_allowance record;

    v_feature_policy jsonb;

    v_feature_start timestamptz;

    v_feature_end timestamptz;

    v_feature_limit integer;

    v_feature_action text;

    v_allowance_start timestamptz;

    v_allowance_end timestamptz;

    v_free numeric := 0;

 v_result record;

 v_existing record;

 v_has_assignment boolean := false;

BEGIN
    IF p_requested < 0 OR p_idempotency_key IS NULL OR p_idempotency_key = '' THEN
        RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'invalid_request';
 RETURN;

    END IF;

    v_account := bursar.account_for_subject(p_subject_id);

    -- Serialising per account makes feature admission and allowance consumption one
    -- financial decision, rather than two independently racy SDK calls.
    PERFORM 1 FROM bursar.credit_accounts WHERE id = v_account FOR UPDATE;

 SELECT c.allowance_requested,c.allowance_covered INTO v_existing
 FROM bursar.credit_usage_charges AS c
 WHERE c.account_id=v_account AND c.idempotency_key=p_idempotency_key;

 IF FOUND THEN
 SELECT * INTO v_result FROM bursar.charge_usage(p_subject_id,p_operation,p_requested,p_idempotency_key,p_feature,p_model,p_region,v_existing.allowance_covered,p_metadata,v_existing.allowance_requested);

        RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;

        RETURN;

    END IF;

    SELECT a.plan_id,a.catalog_revision_id,a.starts_at,p.included_credits,p.included_credits_reset_unit,
           p.included_credits_reset_count,p.included_credits_reset_anchor,p.included_credits_reset_timezone,p.limits
    INTO v_assignment
    FROM bursar.account_plan_assignments a
    JOIN bursar.catalog_plans p ON p.id=a.plan_id AND p.catalog_revision_id=a.catalog_revision_id
 WHERE a.account_id=v_account AND a.starts_at<=now() AND (a.ends_at IS NULL OR a.ends_at>now());

 v_has_assignment := FOUND;

 IF v_has_assignment AND p_feature IS NOT NULL THEN
        v_feature_policy := COALESCE(v_assignment.limits->p_feature, '{}'::jsonb);

        v_feature_limit := NULLIF(v_feature_policy->>'max_calls','')::integer;

        v_feature_action := COALESCE(v_feature_policy->>'action','deny');

        IF v_feature_limit IS NOT NULL THEN
            SELECT window_start,window_end INTO v_feature_start,v_feature_end FROM bursar.policy_period_window(
                v_assignment.starts_at, v_feature_policy #>> '{period,unit}',
                COALESCE((v_feature_policy #>> '{period,count}')::integer,1),
                COALESCE(v_feature_policy #>> '{period,anchor}','calendar'),
                COALESCE(v_feature_policy #>> '{period,timezone}','UTC')
            );

            INSERT INTO bursar.feature_call_windows(account_id,feature,window_start,window_end,limit_value)
            VALUES(v_account,p_feature,v_feature_start,v_feature_end,v_feature_limit)
            ON CONFLICT (account_id,feature,window_start) DO NOTHING;

            IF EXISTS (
                SELECT 1 FROM bursar.feature_call_windows
                WHERE account_id=v_account AND feature=p_feature AND window_start=v_feature_start AND admitted>=v_feature_limit
            ) THEN
                IF v_feature_action='deny' THEN
                    RETURN QUERY SELECT NULL::uuid,NULL::uuid,0::numeric,0::numeric,false,'feature_limit_reached';
 RETURN;

                END IF;

                INSERT INTO bursar.feature_limit_events(account_id,feature,window_start,action,idempotency_key)
                VALUES(v_account,p_feature,v_feature_start,v_feature_action,p_idempotency_key)
                ON CONFLICT DO NOTHING;

            END IF;

        END IF;

    END IF;

 IF v_has_assignment AND v_assignment.included_credits IS NOT NULL THEN
        SELECT window_start,window_end INTO v_allowance_start,v_allowance_end FROM bursar.policy_period_window(
            v_assignment.starts_at, v_assignment.included_credits_reset_unit,
            v_assignment.included_credits_reset_count, v_assignment.included_credits_reset_anchor,
            v_assignment.included_credits_reset_timezone
        );

        INSERT INTO bursar.allowance_windows(account_id,plan_id,catalog_revision_id,feature,window_start,window_end,period_unit,period_count,period_anchor,period_timezone,allowance)
        VALUES(v_account,v_assignment.plan_id,v_assignment.catalog_revision_id,'__included_credits__',v_allowance_start,v_allowance_end,
               v_assignment.included_credits_reset_unit,v_assignment.included_credits_reset_count,v_assignment.included_credits_reset_anchor,v_assignment.included_credits_reset_timezone,v_assignment.included_credits)
        ON CONFLICT (account_id,plan_id,catalog_revision_id,feature,window_start,window_end) DO NOTHING;

        SELECT LEAST(p_requested,GREATEST(allowance-reserved-consumed,0)) INTO v_free
        FROM bursar.allowance_windows WHERE account_id=v_account AND plan_id=v_assignment.plan_id
          AND feature='__included_credits__' AND window_start=v_allowance_start AND window_end=v_allowance_end FOR UPDATE;

    END IF;

    IF v_free > 0 THEN
        SELECT * INTO v_result FROM bursar.charge_usage_with_window(
            p_subject_id,p_operation,p_requested,p_idempotency_key,'__included_credits__',v_allowance_start,v_allowance_end,v_free,
            p_model,p_region,p_metadata,p_feature
        );

    ELSE
        SELECT * INTO v_result FROM bursar.charge_usage(p_subject_id,p_operation,p_requested,p_idempotency_key,p_feature,p_model,p_region,0,p_metadata,0);

    END IF;

    IF v_result.error_code IS NOT NULL THEN
        RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;

        RETURN;

    END IF;

    IF v_feature_limit IS NOT NULL THEN
        UPDATE bursar.feature_call_windows
        SET consumed=consumed+1, admitted=admitted+1
        WHERE account_id=v_account AND feature=p_feature AND window_start=v_feature_start
          AND admitted < v_feature_limit;

    END IF;

    RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;

END $$;
