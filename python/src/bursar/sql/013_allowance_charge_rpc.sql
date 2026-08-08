-- Windowed allowance charge RPCs.
-- Generated from the pre-production Bursar baseline; keep this file self-contained.

CREATE FUNCTION bursar.charge_usage_with_window(
    p_subject_id uuid,
    p_operation text,
    p_requested numeric,
    p_idempotency_key text,
    p_allowance_key text,
    p_window_start timestamptz,
    p_window_end timestamptz,
    p_allowance numeric,
    p_model text DEFAULT NULL,
    p_region text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_charge_feature text DEFAULT NULL,
    p_measures jsonb DEFAULT '{}'::jsonb,
    p_dimensions jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    charge_id uuid,
    ledger_entry_id uuid,
    charged numeric,
    allowance_covered numeric,
    replayed boolean,
    error_code text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
DECLARE v_result record;
 v_allowance numeric;
 v_account uuid;
 v_consumed boolean:=false;

BEGIN
    IF p_subject_id IS NULL
       OR NOT bursar.is_finite_numeric(p_requested)
       OR p_requested < 0
       OR NOT bursar.is_finite_numeric(p_allowance)
       OR p_allowance < 0
       OR NOT bursar.is_nonempty_text(p_operation)
       OR NOT bursar.is_bounded_text(p_operation, 255)
       OR NOT bursar.is_nonempty_text(p_idempotency_key)
       OR NOT bursar.is_bounded_text(p_idempotency_key, 255)
       OR NOT bursar.is_nonempty_text(p_allowance_key)
       OR NOT bursar.is_bounded_text(p_allowance_key, 255)
       OR p_window_start IS NULL
       OR p_window_end IS NULL
       OR p_window_end <= p_window_start
       OR (
           p_charge_feature IS NOT NULL
           AND NOT bursar.is_bounded_text(p_charge_feature, 255)
       )
       OR (
           p_model IS NOT NULL
           AND NOT bursar.is_bounded_text(p_model, 255)
       )
       OR (
           p_region IS NOT NULL
           AND NOT bursar.is_bounded_text(p_region, 255)
       )
       OR NOT bursar.is_bounded_json_object(
           COALESCE(p_metadata, '{}'::jsonb),
           1048576
       )
       OR NOT bursar.valid_measure_object(
           COALESCE(p_measures, '{}'::jsonb),
           16384
       )
       OR NOT bursar.valid_dimension_object(
           COALESCE(p_dimensions, '{}'::jsonb),
           65536
       )
    THEN
        RETURN QUERY
        SELECT
            NULL::uuid,
            NULL::uuid,
            0::numeric,
            0::numeric,
            false,
            'invalid_request'::text;
        RETURN;
    END IF;

    v_allowance:=LEAST(p_requested,GREATEST(p_allowance,0));

    v_account:=bursar.account_for_subject(p_subject_id);

    -- Account-scoped serialization makes the pre-charge idempotency check and
    -- allowance consumption one admission decision. Without it, two first
    -- attempts can both consume allowance before charge_usage sees the replay.
    PERFORM 1
    FROM bursar.credit_accounts
    WHERE id = v_account
    FOR UPDATE;

    IF EXISTS (SELECT 1 FROM bursar.credit_usage_charges WHERE account_id=v_account AND idempotency_key=p_idempotency_key) THEN
        SELECT * INTO v_result
        FROM bursar.charge_usage(
        p_subject_id,p_operation,p_requested,p_idempotency_key,p_charge_feature,p_model,p_region,
        (SELECT c.allowance_covered FROM bursar.credit_usage_charges c WHERE c.account_id=v_account AND c.idempotency_key=p_idempotency_key),
        p_metadata,
        (SELECT c.allowance_requested FROM bursar.credit_usage_charges c WHERE c.account_id=v_account AND c.idempotency_key=p_idempotency_key),
        p_measures => COALESCE(p_measures, '{}'::jsonb),
        p_dimensions => COALESCE(p_dimensions, '{}'::jsonb)
        );

        RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;

        RETURN;

    END IF;

    IF v_allowance>0 AND NOT bursar.consume_allowance(p_subject_id,p_allowance_key,p_window_start,p_window_end,v_allowance) THEN v_allowance:=0;
 END IF;

    v_consumed:=v_allowance>0;

    SELECT * INTO v_result FROM bursar.charge_usage(
        p_subject_id,
        p_operation,
        p_requested,
        p_idempotency_key,
        p_charge_feature,
        p_model,
        p_region,
        v_allowance,
        p_metadata,
        v_allowance,
        p_measures => COALESCE(p_measures, '{}'::jsonb),
        p_dimensions => COALESCE(p_dimensions, '{}'::jsonb)
    );

    IF v_result.error_code IS NOT NULL AND v_consumed THEN
        UPDATE bursar.allowance_windows AS aw
        SET consumed=aw.consumed-v_allowance
        WHERE aw.account_id=v_account AND aw.allowance_key=p_allowance_key AND aw.window_start=p_window_start
          AND aw.window_end=p_window_end AND aw.consumed>=v_allowance;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'allowance rollback is inconsistent'
                USING ERRCODE = '23514';
        END IF;

    END IF;

    RETURN QUERY SELECT v_result.charge_id,v_result.ledger_entry_id,v_result.charged,v_result.allowance_covered,v_result.replayed,v_result.error_code;

END $$;
